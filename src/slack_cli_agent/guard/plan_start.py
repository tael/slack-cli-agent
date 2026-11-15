"""UserPromptSubmit hook: start the engine's plan call alongside the turn.

2026-11-10 W5: plan_gate asked the model for a `[계획]` block and denied the
third tool-calling response without one. All 15 runs ended in two denials
and gave_up. Claude Code 2.1.321 writes a response's text and tool_use lines
to the transcript 50-110 ms after its PreToolUse hook starts, so a plan
written in the same response as the call is invisible to the hook, and the
model wrote no text with its first call in 15 of 15 runs anyway
(tasks/guchipachi-orchestration/plan_gate_rca.md). The plan is now made by
code instead of being asked of the model.

On each request of a turn the pipeline set PLAN_GATE for, this hook removes
the previous request's plan files and starts a detached planner process,
then returns at once. The planner runs a tool-less nested ``claude -p``
(default haiku) with a JSON schema and writes ``<transcript>.plan.json``.
guard/pretool/plan_gate injects the finished plan once as PreToolUse
additionalContext. It never denies, so a plan that arrives late, fails or
says "short" only means nothing is injected.

The plan file is also the record: status is ``ok``, ``timeout``,
``error:<kind>`` or ``bad_output``, never a substitute plan.

Workflow classification (record only). Where the pipeline sets
SCA_PLAN_WORKFLOWS (JSON ``{id: description}``), the same call also picks the
workflow that fits the request's meaning and writes ``workflow``,
``workflow_confidence`` and ``workflow_reason`` into the plan file. No length
or keyword rule: the description of each candidate carries example utterances.
plan_gate never reads these fields, so the injected text and the turn's
behavior are unchanged. On a resumed turn the tail of the earlier conversation
(transcript) is added after the request, because short follow-ups cannot be
read without it.

Standard library only. Never fails the prompt: every path ends in exit 0
with nothing on stdout (UserPromptSubmit stdout would be added to context).
"""

from __future__ import annotations

import contextlib
import json
import re
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HOOK_SCRIPT = Path(__file__).resolve()

#: Same switch as plan_gate: set by the pipeline on turns the flow applies to.
PLAN_GATE_ENV = "PLAN_GATE"
#: Set in the planner's own environment so nothing it starts plans again.
PLAN_NESTED_ENV = "SCA_PLAN_NESTED"
#: Set inside the finish-review verify claude (finish_review.VERIFY_NESTED_ENV).
VERIFY_NESTED_ENV = "SCA_VERIFY_NESTED"
#: Set by the pipeline when this turn's tool list has Agent or Task. Only then
#: may the planner name a worker; otherwise every branch's delegate is "none".
AGENT_DELEGATION_ENV = "AGENT_DELEGATION"
#: Planner model override. haiku measured 4.3-6.6 s for four plans (RCA 재현 6).
PLAN_MODEL_ENV = "SCA_PLAN_MODEL"
DEFAULT_MODEL = "haiku"
#: Planner call limit. Past this the turn is usually well under way.
TIMEOUT_SEC = 20.0
#: Tail of the request handed to the planner. The pipeline's prompt carries
#: thread history before the request itself, so the end matters most.
PROMPT_TAIL_CHARS = 12_000

#: Set by the pipeline: JSON object ``{workflow id: description}`` of the
#: candidates the planner may choose from. Absent or invalid means the planner
#: classifies nothing and the plan file has no workflow fields.
WORKFLOWS_ENV = "SCA_PLAN_WORKFLOWS"
#: The fallback workflow when the planner is not confident.
FALLBACK_WORKFLOW = "deep"
CONFIDENCES = ("low", "medium", "high")
#: Resumed-turn context: the last N text messages of the transcript, each cut
#: to a limit, and a limit on the whole block.
CONTEXT_MESSAGES = 4
CONTEXT_MESSAGE_CHARS = 300
CONTEXT_TOTAL_CHARS = 1_200
#: Only this much of the transcript's end is read to find them.
CONTEXT_READ_BYTES = 400_000

#: Agent worker names the flow note delegates to (profiles/agents/*.md).
WORKERS = ("lookup", "analyst")

def delegation_enabled(env: dict[str, str] | os._Environ[str] | None = None) -> bool:
    """AGENT_DELEGATION on: set and not "0". Absent or empty means off."""
    raw = (env if env is not None else os.environ).get(AGENT_DELEGATION_ENV, "")
    return str(raw).strip() not in ("", "0")


def workflow_candidates(env: dict[str, str] | os._Environ[str] | None = None) -> dict[str, str]:
    """``{id: description}`` from SCA_PLAN_WORKFLOWS; empty when absent or invalid."""
    raw = (env if env is not None else os.environ).get(WORKFLOWS_ENV, "")
    if not str(raw).strip():
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        k: " ".join(v.split()) for k, v in value.items()
        if isinstance(k, str) and k and isinstance(v, str) and v.strip()
    }


def schema(delegation: bool = False, workflows: Any = None) -> dict[str, Any]:
    """The planner's JSON schema. Workers are enum values only with delegation.
    Workflow fields are there only when candidate ids are given."""
    result = _base_schema(delegation)
    if workflows:
        result["properties"]["workflow"] = {"type": "string", "enum": list(workflows)}
        result["properties"]["workflow_confidence"] = {"type": "string", "enum": list(CONFIDENCES)}
        result["properties"]["workflow_reason"] = {"type": "string"}
        result["required"] = [*result["required"], "workflow", "workflow_confidence", "workflow_reason"]
    return result


def _base_schema(delegation: bool) -> dict[str, Any]:
    workers = list(WORKERS) if delegation else []
    return {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["short", "branch", "explore"]},
            "branches": {
                "type": "array",
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string"},
                        "source": {"type": "string"},
                        "depends_on": {"type": "array", "items": {"type": "integer"}},
                        "delegate": {"type": "string", "enum": ["none", *workers]},
                        "returns": {"type": "string"},
                    },
                    "required": ["goal", "source", "depends_on", "delegate", "returns"],
                },
            },
        },
        "required": ["type", "branches"],
    }


_PLANNER_HEAD = (
    "너는 슬랙 업무 봇의 작업 계획만 세운다. 도구는 없다. 아래 [요청]을 읽고 JSON 으로만 답한다.\n"
    "1. type 을 정한다\n"
    "- short : 조회 1-2회로 답이 나온다. branches 는 빈 배열이다\n"
    "- 5why, 부검, RCA, 근본원인, 장애·오류 원인 분석, 재발방지 요청은 short 가 아니다. "
    "원천별(로그, 메트릭, k8s 이벤트, DB, 코드, 슬랙) 갈래로 나눠 branch 로 적는다\n"
    "- branch : 서로 독립된 조회나 작업이 둘 이상이다. branches 는 2-5개다\n"
    "- explore : 무엇이 문제인지 모르고 앞 결과가 다음 조회를 정한다. branches 는 첫 갈래 1-2개만 적는다\n"
    "2. 갈래마다 적는다\n"
    "- goal : 알아낼 것 한 문장\n"
    "- source : 볼 원천. 테이블, 파일, API, 채널 중 요청에 나온 것. 모르면 '확인 필요'\n"
    "- depends_on : 먼저 끝나야 하는 갈래 번호(1부터). 없으면 빈 배열\n"
)
_DELEGATE_WITH_WORKERS = (
    "- delegate : 도구 호출이 3회 이상 들 갈래만 작업자에게 맡긴다. 조회만이면 lookup, "
    "집계 스크립트 실행이 필요하면 analyst, 그보다 작으면 none\n"
)
_DELEGATE_NONE = "- delegate : 항상 none 으로 적는다. 이 턴에는 작업자가 없다\n"
_PLANNER_TAIL = (
    "- returns : 돌려받을 것. 수치, 목록, 판정 중 하나와 그 대상\n"
    "3. 요청에 없는 작업을 만들지 않는다. 쓰기, 게시, 삭제가 요청에 없으면 갈래에 넣지 않는다\n\n"
    "[요청]\n"
)


_WORKFLOW_HEAD = (
    "4. workflow 를 고른다. 아래 후보 중 요청의 의미에 맞는 id 하나다\n"
    "- 슬랙 질문은 대부분 짧다. 글자 수나 낱말로 정하지 않는다. 무엇을 알고 싶은지 추정해 고른다\n"
    "- 짧은 질문이 가벼운 경우는 드물다. 대상이 모호하거나 직전 대화에 기대는 후속 발화는 "
    "[직전 대화]가 있으면 그 맥락으로 읽는다\n"
    "- workflow_confidence 는 low, medium, high 중 하나다. 확신이 낮으면 workflow 는 "
    f"{FALLBACK_WORKFLOW} 로 하고 low 로 적는다\n"
    "- workflow_reason 은 고른 근거 한 문장이며 40자 이내다. 길게 쓰면 계획이 늦어진다\n"
    "[workflow 후보]\n"
)


def workflow_section(workflows: dict[str, str]) -> str:
    if not workflows:
        return ""
    return _WORKFLOW_HEAD + "".join(f"- {wid} : {desc}\n" for wid, desc in workflows.items())


def planner_prompt(delegation: bool = False, workflows: dict[str, str] | None = None) -> str:
    """The instruction placed before the request. The workflow section goes
    after item 3 and before the [요청] marker; absent candidates leave the
    text exactly as it was."""
    tail = _PLANNER_TAIL
    if workflows:
        marker = "\n[요청]\n"
        head, _, rest = tail.partition(marker)
        tail = head + workflow_section(workflows) + marker + rest
    return _PLANNER_HEAD + (_DELEGATE_WITH_WORKERS if delegation else _DELEGATE_NONE) + tail


#: Without delegation, the default.
SCHEMA: dict[str, Any] = schema(False)
PLANNER_PROMPT = planner_prompt(False)


#: Root-cause requests are never "short". On 2026-11-13 haiku planned a
#: "서버 부하 관점으로 5why" request as short with no branches, so no plan was
#: injected and the turn ran without workers (김태일 지시 "5why를 short 단답형
#: 으로 간주하면 안됨"). The prompt rule above asks for it; this pattern is the
#: final check: a short verdict on such a request gets one more planner call.
RCA_PATTERN = re.compile(
    r"5\s*-?\s*why|부검|rca\b|근본\s*원인|재발\s*방지|원인\s*(분석|규명|조사)",
    re.IGNORECASE,
)
RCA_RETRY_NOTE = (
    "\n\n[계획 재요청] 이 요청은 원인 분석이다. short 로 답하지 말고 "
    "원천별 갈래 2-5개를 branch 로 적는다.\n"
)


def is_rca(prompt: str) -> bool:
    return bool(RCA_PATTERN.search(prompt))


def plan_path(transcript_path: str) -> Path:
    return Path(transcript_path + ".plan.json")


def injected_path(transcript_path: str) -> Path:
    return Path(transcript_path + ".plan.injected")


def prompt_path(transcript_path: str) -> Path:
    return Path(transcript_path + ".plan.prompt")


def context_path(transcript_path: str) -> Path:
    return Path(transcript_path + ".plan.context")


def _message_text(message: Any, role: str) -> str:
    """The person-readable text of one transcript message, "" for tool traffic.

    A user message carries the pipeline's header before the request, so its
    end is kept; an assistant message keeps its start.
    """
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        )
    if not isinstance(content, str):
        return ""
    text = " ".join(content.split())
    if text.startswith("Stop hook feedback:"):
        return ""
    if len(text) > CONTEXT_MESSAGE_CHARS:
        text = "…" + text[-CONTEXT_MESSAGE_CHARS:] if role == "user" else text[:CONTEXT_MESSAGE_CHARS] + "…"
    return text


def recent_context(transcript_path: str, prompt: str) -> str:
    """The tail of the earlier conversation, or "" on a first turn.

    Reads the transcript's last bytes and keeps the last CONTEXT_MESSAGES text
    messages of the user and the bot, oldest first. A user message that is the
    current prompt (the CLI may have written it already) is left out.
    """
    try:
        with open(transcript_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - CONTEXT_READ_BYTES))
            raw = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    lines = raw.splitlines()
    if size > CONTEXT_READ_BYTES:
        lines = lines[1:]  # the first line is cut mid-way
    current = " ".join(prompt.split())[-80:]
    found: list[tuple[str, str]] = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("isSidechain"):
            continue
        role = event.get("type")
        if role not in ("user", "assistant"):
            continue
        text = _message_text(event.get("message"), role)
        if not text:
            continue
        if role == "user" and current and text.endswith(current[-60:]):
            continue
        found.append((role, text))
    chosen = found[-CONTEXT_MESSAGES:]
    if not chosen:
        return ""
    label = {"user": "사용자", "assistant": "봇"}
    block = "\n".join(f"{label[role]}: {text}" for role, text in chosen)
    return block[-CONTEXT_TOTAL_CHARS:]


def enabled(env: dict[str, str] | os._Environ[str] | None = None) -> bool:
    source = env if env is not None else os.environ
    if source.get(PLAN_NESTED_ENV) or source.get(VERIFY_NESTED_ENV):
        return False
    try:
        return int(str(source.get(PLAN_GATE_ENV, "")).strip() or 0) > 0
    except ValueError:
        return False


def _write_json(path: Path, value: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


#: Env var that makes start() plan inside the hook and return the plan as the
#: prompt's additionalContext, before the first model response. Absent, empty
#: or "0" keeps the detached planner. In A' replay (2026-11-16) the detached
#: plan arrived after 2-5 direct tool calls and 2 of 9 runs called Agent twice
#: in one response (tasks/shinji-workflow/h8-force-parallel/RESULT.md).
PLAN_SYNC_ENV = "SCA_PLAN_SYNC"
#: Per planner call in sync mode. The prompt waits for it; planner elapsed was
#: at most 13.5 s over 279 plan files (2026-11-16).
SYNC_TIMEOUT_SEC = 15.0
#: guard/pretool/plan_gate.py, loaded by path: both files run as scripts.
PLAN_GATE_SCRIPT = Path(__file__).resolve().parent / "pretool" / "plan_gate.py"


def sync_enabled(env: dict[str, str] | os._Environ[str] | None = None) -> bool:
    raw = (env if env is not None else os.environ).get(PLAN_SYNC_ENV, "")
    return str(raw).strip() not in ("", "0")


def _gate_context(transcript: str) -> str:
    """plan_gate.decide on the finished plan: the injected text, or "" when it
    does not inject. decide also writes the injected flag, so the PreToolUse
    gate does not inject a second time."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_plan_gate_sync", PLAN_GATE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    verdict, context = module.decide({"transcript_path": transcript})
    return context if verdict == "inject" else ""


def start(data: dict[str, Any], *, spawn: Any = subprocess.Popen, runner: Any = None) -> str:
    """Reset this request's plan files and launch the planner. Returns what
    it did, for tests and the hook's silence."""
    if not enabled():
        return "off"
    if data.get("agent_id"):
        return "subagent"
    transcript = data.get("transcript_path")
    prompt = data.get("prompt")
    if not transcript or not isinstance(prompt, str) or not prompt.strip():
        return "no_input"
    for stale in (plan_path(transcript), injected_path(transcript), context_path(transcript)):
        stale.unlink(missing_ok=True)
    Path(transcript).parent.mkdir(parents=True, exist_ok=True)
    prompt_path(transcript).write_text(prompt[-PROMPT_TAIL_CHARS:], encoding="utf-8")
    if workflow_candidates():  # classification reads context; plans without it stay as before
        context = recent_context(transcript, prompt)
        if context:
            context_path(transcript).write_text(context, encoding="utf-8")
    _write_json(plan_path(transcript), {"status": "pending", "started_at": time.time()})
    if sync_enabled():
        (runner or run)(transcript, timeout=SYNC_TIMEOUT_SEC)
        return "sync"
    spawn(
        [sys.executable, str(HOOK_SCRIPT), "--run", transcript],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, env={**os.environ, PLAN_NESTED_ENV: "1"},
    )
    return "started"


def _call_planner(prompt: str, model: str, timeout: float) -> subprocess.CompletedProcess[str]:
    delegation = delegation_enabled()
    workflows = workflow_candidates()
    exe = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    cmd = [
        exe, "-p", "--model", model, "--tools", "", "--strict-mcp-config",
        "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
        "--output-format", "json", "--json-schema", json.dumps(schema(delegation, workflows)),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = proc.communicate(input=planner_prompt(delegation, workflows) + prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def parse(stdout: str) -> tuple[str, dict[str, Any] | None]:
    """(status, plan) from the CLI's ``--output-format json`` result.

    The CLI emits either a single result object or, with verbose on, an array
    of events ending in one (engine/claude.py _result_event has the same rule).
    2026-11-16: the operator's verbose setting reached this call despite
    --setting-sources "", and all 26 recorded plans were error:cli.
    """
    try:
        result = json.loads(stdout)
    except ValueError:
        return "bad_output", None
    if isinstance(result, list):
        result = next(
            (e for e in reversed(result) if isinstance(e, dict) and e.get("type") == "result"),
            None,
        )
        if result is None:
            return "bad_output", None
    if not isinstance(result, dict) or result.get("is_error"):
        return "error:cli", None
    plan = result.get("structured_output")
    if not isinstance(plan, dict) or plan.get("type") not in ("short", "branch", "explore"):
        return "bad_output", None
    branches = plan.get("branches")
    if not isinstance(branches, list):
        return "bad_output", None
    plan["cost_usd"] = result.get("total_cost_usd")
    return "ok", plan


WORKFLOW_FIELDS = ("workflow", "workflow_confidence", "workflow_reason")


def check_workflow(plan: dict[str, Any], candidates: dict[str, str]) -> str:
    """Keep the workflow fields only where they are valid; return a note.

    "" means they are valid, "off" means no candidates were given (fields
    removed), "invalid" means the id or confidence is outside what was offered
    (fields removed, so a wrong value is never recorded as a classification).
    """
    if not candidates:
        for key in WORKFLOW_FIELDS:
            plan.pop(key, None)
        return "off"
    reason = plan.get("workflow_reason")
    if (
        plan.get("workflow") in candidates
        and plan.get("workflow_confidence") in CONFIDENCES
        and isinstance(reason, str)
    ):
        return ""
    for key in WORKFLOW_FIELDS:
        plan.pop(key, None)
    return "invalid"


def run(transcript: str, *, caller: Any = _call_planner, timeout: float = TIMEOUT_SEC) -> dict[str, Any]:
    """Planner process body: call the model and write the plan file."""
    model = os.environ.get(PLAN_MODEL_ENV) or DEFAULT_MODEL
    started = time.monotonic()
    record: dict[str, Any] = {"model": model}
    try:
        request = prompt_path(transcript).read_text(encoding="utf-8")
        candidates = workflow_candidates()
        prompt = request
        if candidates and context_path(transcript).exists():
            context = context_path(transcript).read_text(encoding="utf-8")
            prompt = f"{request}\n\n[직전 대화 : 참고용이며 요청이 아니다]\n{context}\n"
        proc = caller(prompt, model, timeout)
        status, plan = parse(proc.stdout or "")
        if plan is not None and plan.get("type") == "short" and is_rca(request):
            record["rca_retry"] = True
            proc = caller(prompt + RCA_RETRY_NOTE, model, timeout)
            status, plan = parse(proc.stdout or "")
        if plan is not None:
            note = check_workflow(plan, candidates)
            if note == "invalid":
                record["workflow_error"] = "invalid"
            record.update(plan)
    except subprocess.TimeoutExpired:
        status = "timeout"
    except Exception as exc:  # noqa: BLE001 - the plan file records the failure
        status = "error:" + type(exc).__name__
    record["status"] = status
    record["elapsed_sec"] = round(time.monotonic() - started, 1)
    record["finished_at"] = time.time()
    try:
        _write_json(plan_path(transcript), record)
    finally:
        prompt_path(transcript).unlink(missing_ok=True)
        context_path(transcript).unlink(missing_ok=True)
    return record


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[1] == "--run":
        with contextlib.suppress(Exception):  # detached; nothing to report to
            run(argv[2])
        return 0
    with contextlib.suppress(Exception):  # never fail the prompt
        data = json.load(sys.stdin)
        if start(data) == "sync":
            context = _gate_context(data["transcript_path"])
            if context:
                print(json.dumps({
                    "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context},
                }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
