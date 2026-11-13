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
#: Planner model override. haiku measured 4.3-6.6 s for four plans (RCA 재현 6).
PLAN_MODEL_ENV = "SCA_PLAN_MODEL"
DEFAULT_MODEL = "haiku"
#: Planner call limit. Past this the turn is usually well under way.
TIMEOUT_SEC = 20.0
#: Tail of the request handed to the planner. The pipeline's prompt carries
#: thread history before the request itself, so the end matters most.
PROMPT_TAIL_CHARS = 12_000

#: Agent worker names the flow note delegates to (profiles/agents/*.md).
WORKERS = ("lookup", "analyst")

SCHEMA: dict[str, Any] = {
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
                    "delegate": {"type": "string", "enum": ["none", *WORKERS]},
                    "returns": {"type": "string"},
                },
                "required": ["goal", "source", "depends_on", "delegate", "returns"],
            },
        },
    },
    "required": ["type", "branches"],
}

PLANNER_PROMPT = (
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
    "- delegate : 도구 호출이 3회 이상 들 갈래만 작업자에게 맡긴다. 조회만이면 lookup, "
    "집계 스크립트 실행이 필요하면 analyst, 그보다 작으면 none\n"
    "- returns : 돌려받을 것. 수치, 목록, 판정 중 하나와 그 대상\n"
    "3. 요청에 없는 작업을 만들지 않는다. 쓰기, 게시, 삭제가 요청에 없으면 갈래에 넣지 않는다\n\n"
    "[요청]\n"
)


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


def start(data: dict[str, Any], *, spawn: Any = subprocess.Popen) -> str:
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
    for stale in (plan_path(transcript), injected_path(transcript)):
        stale.unlink(missing_ok=True)
    Path(transcript).parent.mkdir(parents=True, exist_ok=True)
    prompt_path(transcript).write_text(prompt[-PROMPT_TAIL_CHARS:], encoding="utf-8")
    _write_json(plan_path(transcript), {"status": "pending", "started_at": time.time()})
    spawn(
        [sys.executable, str(HOOK_SCRIPT), "--run", transcript],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, env={**os.environ, PLAN_NESTED_ENV: "1"},
    )
    return "started"


def _call_planner(prompt: str, model: str, timeout: float) -> subprocess.CompletedProcess[str]:
    exe = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    cmd = [
        exe, "-p", "--model", model, "--tools", "", "--strict-mcp-config",
        "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
        "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = proc.communicate(input=PLANNER_PROMPT + prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def parse(stdout: str) -> tuple[str, dict[str, Any] | None]:
    """(status, plan) from the CLI's ``--output-format json`` result."""
    try:
        result = json.loads(stdout)
    except ValueError:
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


def run(transcript: str, *, caller: Any = _call_planner) -> dict[str, Any]:
    """Planner process body: call the model and write the plan file."""
    model = os.environ.get(PLAN_MODEL_ENV) or DEFAULT_MODEL
    started = time.monotonic()
    record: dict[str, Any] = {"model": model}
    try:
        prompt = prompt_path(transcript).read_text(encoding="utf-8")
        proc = caller(prompt, model, TIMEOUT_SEC)
        status, plan = parse(proc.stdout or "")
        if plan is not None and plan.get("type") == "short" and is_rca(prompt):
            record["rca_retry"] = True
            proc = caller(prompt + RCA_RETRY_NOTE, model, TIMEOUT_SEC)
            status, plan = parse(proc.stdout or "")
        if plan is not None:
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
    return record


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[1] == "--run":
        with contextlib.suppress(Exception):  # detached; nothing to report to
            run(argv[2])
        return 0
    with contextlib.suppress(Exception):  # never fail the prompt
        start(json.load(sys.stdin))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
