"""Step ledger: a plan's small ordered steps, their state, and the approval gate.

guard/plan_start asks the planner for ``steps`` when the pipeline set
SCA_PLAN_STEPS and writes them here as ``<transcript>.plan.steps.json``.
guard/pretool/plan_gate injects the steps with the commands below, and the
main session (or the executor subagent it hands the steps to) marks each
step as it goes:

    <python> plan_steps.py --ledger <path> show
    <python> plan_steps.py --ledger <path> start N
    <python> plan_steps.py --ledger <path> done N [메모]
    <python> plan_steps.py --ledger <path> fail N 사유
    <python> plan_steps.py --ledger <path> approve
    <python> plan_steps.py --ledger <path> cancel

``fail`` replans the remaining steps with one tool-less planner call, up to
MAX_REPLANS times; past that the ledger halts and the step must be reported.

Approval is enforced in code, not asked of the model (2026-11-28 owner
decision 4A: payment, send and delete steps wait for approval in Slack):

- ``start`` refuses an approval step that is not approved
- ``approve`` is refused in the turn that wrote the ledger and on any tier
  other than owner, so the turn that reaches an approval step always ends
  and asks first
- the PreToolUse gate (``--gate``) denies non-read tools in that turn once
  the next step needs approval, and denies the browser skill's
  ``--approved`` flag on every turn until a step is approved

Standard library only; runs as a script by path. The gate never fails the
tool call: every error path ends in exit 0 with nothing printed.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HOOK_SCRIPT = Path(__file__).resolve()
PLAN_START_SCRIPT = HOOK_SCRIPT.parent / "plan_start.py"

STEPS_ENV = "SCA_PLAN_STEPS"
SKILLS_ENV = "SCA_PLAN_SKILLS"
STEPS_MAX = 12
SKILLS_MAX = 30
SKILL_DESC_CHARS = 160
MAX_REPLANS = 2
#: A ledger waiting for approval may wait for hours; past a day it is dropped.
STALE_SEC = 24 * 3600.0
REPLAN_TIMEOUT_SEC = 30.0
OWNER_TIERS = frozenset({"owner", "owner_trusted"})
READ_TOOLS = frozenset({"Read", "Grep", "Glob", "WebSearch", "WebFetch", "TodoWrite", "ToolSearch"})
APPROVED_FLAG = "--approved"
#: 2026-11-28 실제 사이트 시험 : 메인이 Playwright 스크립트를 직접 짜 browser 스킬의 승인
#: 검사와 장부를 건너뛰고 주문을 완료했다. 장부가 살아 있는 동안 브라우저를 직접 다루는
#: 코드는 막는다. 브라우저 동작은 browser 스킬 명령으로만 한다.
DIRECT_BROWSER_MARKERS = ("playwright", "puppeteer", "connectovercdp", "chromium.connect", "9333", "devtools")

PENDING, RUNNING, DONE, FAILED, SKIPPED = "pending", "running", "done", "failed", "skipped"
FINISHED = frozenset({DONE, SKIPPED})


def steps_enabled(env: dict[str, str] | os._Environ[str] | None = None) -> bool:
    raw = (env if env is not None else os.environ).get(STEPS_ENV, "")
    return str(raw).strip() not in ("", "0")


def skills_list(env: dict[str, str] | os._Environ[str] | None = None) -> list[dict[str, str]]:
    """``[{name, description}]`` from SCA_PLAN_SKILLS; empty when absent or invalid."""
    raw = (env if env is not None else os.environ).get(SKILLS_ENV, "")
    try:
        value = json.loads(raw) if str(raw).strip() else []
    except ValueError:
        return []
    found = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip():
            desc = str(item.get("description") or "")
            found.append({"name": item["name"].strip(), "description": " ".join(desc.split())[:SKILL_DESC_CHARS]})
    return found[:SKILLS_MAX]


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    fields: dict[str, str] = {}
    for line in text[3:end].splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() and not line.startswith((" ", "\t")):
            fields[key.strip()] = value.strip().strip("'\"")
    return fields


def scan_skills(root: Path) -> list[dict[str, str]]:
    """Skills under ``root`` (``<name>/SKILL.md``, one level, symlinks followed),
    as name and description from the frontmatter. Sorted by name."""
    found: dict[str, dict[str, str]] = {}
    with contextlib.suppress(OSError):
        for skill_md in sorted(root.glob("**/SKILL.md")):
            if len(skill_md.relative_to(root).parts) > 4:
                continue
            with contextlib.suppress(OSError, UnicodeDecodeError):
                meta = _frontmatter(skill_md.read_text(encoding="utf-8"))
                name = meta.get("name") or skill_md.parent.name
                if name not in found:
                    found[name] = {
                        "name": name,
                        "description": " ".join(meta.get("description", "").split())[:SKILL_DESC_CHARS],
                        "path": str(skill_md),
                    }
    return [found[k] for k in sorted(found)][:SKILLS_MAX]


def ledger_path(transcript_path: str) -> Path:
    return Path(transcript_path + ".plan.steps.json")


def _write(path: Path, ledger: dict[str, Any]) -> None:
    ledger["updated_at"] = time.time()
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def read(path: Path, now: float | None = None) -> dict[str, Any] | None:
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(ledger, dict) or not isinstance(ledger.get("steps"), list):
        return None
    stamp = ledger.get("updated_at") or ledger.get("created_at") or 0
    if not isinstance(stamp, (int, float)) or (now or time.time()) - stamp > STALE_SEC:
        return None
    return ledger


def clean_steps(raw: Any) -> list[dict[str, Any]]:
    """Planner steps as ledger steps; malformed entries are dropped."""
    steps = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("do"), str) or not item["do"].strip():
            continue
        tool = str(item.get("tool") or "")
        steps.append({
            "do": " ".join(item["do"].split()), "tool": tool.strip() or "확인 필요",
            "approval": bool(item.get("approval")), "status": PENDING, "note": "",
        })
    for i, step in enumerate(steps[:STEPS_MAX], start=1):
        step["n"] = i
    return steps[:STEPS_MAX]


def create(
    path: Path, steps: list[dict[str, Any]], *, request: str, turn: str, skills: list[dict[str, str]],
) -> dict[str, Any]:
    now = time.time()
    ledger = {
        "created_at": now, "turn": turn, "request": request[-4000:], "steps": steps,
        "replans": 0, "status": "active", "approved_turn": "",
        "skills": [{"name": s["name"], "description": s.get("description", "")} for s in skills],
    }
    _write(path, ledger)
    return ledger


def next_step(ledger: dict[str, Any]) -> dict[str, Any] | None:
    for step in ledger.get("steps") or []:
        if isinstance(step, dict) and step.get("status") not in FINISHED:
            return step
    return None


def waiting_approval(ledger: dict[str, Any]) -> dict[str, Any] | None:
    """The next step when it needs approval and has none; else None."""
    if ledger.get("status") != "active":
        return None
    step = next_step(ledger)
    if step and step.get("approval") and not step.get("approved"):
        return step
    return None


def any_approved(ledger: dict[str, Any]) -> bool:
    return ledger.get("status") == "active" and any(
        isinstance(s, dict) and s.get("approved") for s in ledger.get("steps") or []
    )


def progress_label(ledger: dict[str, Any]) -> str:
    """"2/5 로그인 버튼 클릭" for the running step, else ""."""
    steps = [s for s in ledger.get("steps") or [] if isinstance(s, dict)]
    for step in steps:
        if step.get("status") == RUNNING:
            return f"{step.get('n')}/{len(steps)} {step.get('do', '')}"[:120]
    return ""


def render(ledger: dict[str, Any]) -> str:
    marks = {PENDING: " ", RUNNING: ">", DONE: "v", FAILED: "x", SKIPPED: "-"}
    lines = []
    for step in ledger.get("steps") or []:
        flag = ""
        if step.get("approval"):
            flag = " [승인됨]" if step.get("approved") else " [승인 필요]"
        note = f" : {step['note']}" if step.get("note") else ""
        mark = marks.get(step.get("status"), "?")
        lines.append(f"[{mark}] {step.get('n')}. {step.get('do')} ({step.get('tool')}){flag}{note}")
    lines.append(f"상태 : {ledger.get('status')} / 재계획 {ledger.get('replans', 0)}/{MAX_REPLANS}")
    return "\n".join(lines)


def command_prefix(path: Path) -> str:
    return " ".join(shlex.quote(p) for p in (sys.executable, str(HOOK_SCRIPT), "--ledger", str(path)))


# ---- commands -------------------------------------------------------------


class StepError(Exception):
    """A refused command; the message is shown to the model."""


def _step(ledger: dict[str, Any], n: int) -> dict[str, Any]:
    for step in ledger.get("steps") or []:
        if isinstance(step, dict) and step.get("n") == n:
            found: dict[str, Any] = step
            return found
    raise StepError(f"{n}단계가 없다")


def cmd_start(ledger: dict[str, Any], n: int) -> str:
    if ledger.get("status") != "active":
        raise StepError(f"장부 상태가 {ledger.get('status')} 이다. 멈추고 보고한다")
    step = _step(ledger, n)
    earlier = [s for s in ledger["steps"] if s["n"] < n and s.get("status") not in FINISHED]
    if earlier:
        raise StepError(f"{earlier[0]['n']}단계가 끝나지 않았다. 순서대로 진행한다")
    if step.get("approval") and not step.get("approved"):
        raise StepError(
            f"{n}단계는 소유자 승인이 필요하다. 지금 멈추고 승인을 구한다. "
            "승인은 소유자의 다음 말에서 approve 로 기록한다"
        )
    step["status"] = RUNNING
    step["started_at"] = time.time()
    return f"{n}단계 시작 : {step['do']}"


def cmd_done(ledger: dict[str, Any], n: int, note: str) -> str:
    step = _step(ledger, n)
    # 2026-11-28 실제 사이트 시험 : start 없이 done 을 몰아 불러 승인 단계가 승인 없이 완료됐다.
    if step.get("status") != RUNNING:
        raise StepError(f"{n}단계는 시작되지 않았다. start {n} 을 먼저 부른다")
    step["status"] = DONE
    step["note"] = note[:200]
    step["finished_at"] = time.time()
    nxt = next_step(ledger)
    if nxt is None:
        ledger["status"] = "done"
        return f"{n}단계 완료. 모든 단계가 끝났다"
    tail = " (승인 필요 : 여기서 멈추고 승인을 구한다)" if nxt.get("approval") and not nxt.get("approved") else ""
    return f"{n}단계 완료. 다음 {nxt['n']}단계 : {nxt['do']}{tail}"


def cmd_approve(ledger: dict[str, Any], env: dict[str, str] | os._Environ[str]) -> str:
    if (env.get("TURN_TIER") or "").strip() not in OWNER_TIERS:
        raise StepError("승인은 소유자 턴에서만 기록한다")
    turn = (env.get("TURN_ID") or "").strip()
    if not turn or turn == ledger.get("turn"):
        raise StepError("계획을 세운 턴에서는 승인할 수 없다. 답을 끝내고 소유자의 다음 말을 기다린다")
    step = waiting_approval(ledger)
    if step is None:
        raise StepError("승인을 기다리는 단계가 없다")
    step["approved"] = True
    step["approved_turn"] = turn
    ledger["approved_turn"] = turn
    return f"{step['n']}단계 승인 기록 : {step['do']}"


def cmd_cancel(ledger: dict[str, Any]) -> str:
    ledger["status"] = "cancelled"
    return "남은 단계를 취소했다"


#: 2026-11-28 owner decision 4A: only payment, send and delete steps wait.
APPROVAL_RULE = (
    "- approval : 결제·구매 확정·송금, 제3자에게 가는 발송(메일, 다른 채널 메시지), 파일·데이터 삭제 단계만 true 다. "
    "조회, 입력, 페이지 이동, 로그인, git 커밋과 push 는 false 다\n"
)


def _replan_prompt(ledger: dict[str, Any], failed: dict[str, Any], reason: str) -> str:
    done = [f"- {s['n']}. {s['do']} : {s.get('note') or '완료'}" for s in ledger["steps"] if s.get("status") == DONE]
    skills = "\n".join(f"- {s['name']} : {s.get('description', '')}" for s in ledger.get("skills") or [])
    return (
        "너는 슬랙 업무 봇의 단계 재계획만 한다. 도구는 없다. JSON 으로만 답한다.\n"
        "[요청]의 남은 일을 작고 순서 있는 단계로 다시 나눈다. 끝난 단계는 다시 넣지 않는다.\n"
        "- do : 한 단계에 행동 하나. 예: 로그인 페이지 열기, 사번 입력, 수량 2 입력\n"
        "- tool : 쓸 도구나 스킬 이름. [쓸 수 있는 도구]에서 고른다\n"
        + APPROVAL_RULE +
        "- 실패 원인을 피하는 다른 방법을 먼저 넣는다. 같은 방법을 되풀이하지 않는다\n"
        f"[쓸 수 있는 도구]\n{skills or '- 확인 필요'}\n"
        f"[끝난 단계]\n{chr(10).join(done) or '- 없음'}\n"
        f"[실패한 단계] {failed['n']}. {failed['do']} ({failed.get('tool')})\n[실패 사유] {reason}\n"
        f"[요청]\n{ledger.get('request', '')}\n"
    )


STEP_SCHEMA = {
    "type": "array",
    "maxItems": STEPS_MAX,
    "items": {
        "type": "object",
        "properties": {"do": {"type": "string"}, "tool": {"type": "string"}, "approval": {"type": "boolean"}},
        "required": ["do", "tool", "approval"],
    },
}


def _call_replanner(prompt: str) -> list[dict[str, Any]]:
    import importlib.util

    spec = importlib.util.spec_from_file_location("_plan_start_replan", PLAN_START_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    model = os.environ.get(module.PLAN_MODEL_ENV) or module.DEFAULT_MODEL
    schema = {"type": "object", "properties": {"steps": STEP_SCHEMA}, "required": ["steps"]}
    proc = module._call_claude(prompt, model, REPLAN_TIMEOUT_SEC, schema)
    result = module.result_event(proc.stdout or "")
    if not isinstance(result, dict) or result.get("is_error"):
        raise StepError("재계획 호출이 실패했다")
    out = result.get("structured_output")
    return clean_steps(out.get("steps") if isinstance(out, dict) else None)


def cmd_fail(ledger: dict[str, Any], n: int, reason: str, *, replanner: Any = _call_replanner) -> str:
    step = _step(ledger, n)
    step["status"] = FAILED
    step["note"] = reason[:200]
    if ledger.get("replans", 0) >= MAX_REPLANS:
        ledger["status"] = "halted"
        return (
            f"{n}단계 실패. 재계획 상한 {MAX_REPLANS}회에 닿았다. "
            "더 시도하지 말고 멈춰서 실패 단계와 사유를 보고한다"
        )
    ledger["replans"] = ledger.get("replans", 0) + 1
    try:
        fresh = replanner(_replan_prompt(ledger, step, reason))
    except (StepError, subprocess.SubprocessError, OSError) as exc:
        ledger["status"] = "halted"
        return f"{n}단계 실패. 재계획을 만들지 못했다({exc}). 멈춰서 보고한다"
    if not fresh:
        ledger["status"] = "halted"
        return f"{n}단계 실패. 재계획이 비었다. 멈춰서 보고한다"
    kept = [s for s in ledger["steps"] if s.get("status") in FINISHED]
    history = ledger.setdefault("failed_steps", [])
    history.append({k: step.get(k) for k in ("do", "tool", "note")})
    for i, new in enumerate(fresh, start=len(kept) + 1):
        new["n"] = i
    ledger["steps"] = kept + fresh
    return f"{n}단계 실패. 남은 단계를 다시 계획했다 ({ledger['replans']}/{MAX_REPLANS})\n" + render(ledger)


def run_command(path: Path, argv: list[str], env: dict[str, str] | os._Environ[str] | None = None) -> tuple[int, str]:
    env = env if env is not None else os.environ
    ledger = read(path)
    if ledger is None:
        return 1, "단계 장부가 없다"
    name = argv[0] if argv else "show"
    try:
        if name == "show":
            return 0, render(ledger)
        if name in ("start", "done", "fail"):
            if len(argv) < 2 or not argv[1].isdigit():
                raise StepError(f"사용법 : {name} <단계 번호>")
            n, text = int(argv[1]), " ".join(argv[2:]).strip()
            if name == "start":
                out = cmd_start(ledger, n)
            elif name == "done":
                out = cmd_done(ledger, n, text)
            else:
                out = cmd_fail(ledger, n, text or "사유 없음")
        elif name == "approve":
            out = cmd_approve(ledger, env)
        elif name == "cancel":
            out = cmd_cancel(ledger)
        else:
            raise StepError(f"모르는 명령 : {name}")
    except StepError as exc:
        return 3, f"거부 : {exc}"
    _write(path, ledger)
    return 0, out


# ---- PreToolUse gate ------------------------------------------------------


def _is_ledger_command(command: str) -> bool:
    return str(HOOK_SCRIPT) in command or "plan_steps.py" in command


def _direct_browser(tool: str, tool_input: dict[str, Any], command: str) -> bool:
    if tool == "Bash":
        text = command
    elif tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        text = " ".join(str(tool_input.get(k) or "") for k in ("content", "new_string", "new_source", "edits"))
    else:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in DIRECT_BROWSER_MARKERS)


def gate(data: dict[str, Any], env: dict[str, str] | os._Environ[str] | None = None) -> str:
    """The deny reason, or "" to let the call through."""
    env = env if env is not None else os.environ
    transcript = data.get("transcript_path")
    if not transcript:
        return ""
    ledger = read(ledger_path(transcript))
    if ledger is None:
        return ""
    tool = str(data.get("tool_name") or "")
    raw_input = data.get("tool_input")
    tool_input: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
    command = str(tool_input.get("command") or "") if tool == "Bash" else ""
    if command and _is_ledger_command(command):
        return ""
    if ledger.get("status") == "active" and _direct_browser(tool, tool_input, command):
        return (
            "단계 실행 중에는 브라우저를 직접 다루는 스크립트를 쓰거나 실행하지 않는다. browser 스킬 명령"
            "(browser.mjs)으로 한 동작씩 하고, 막히면 fail 로 기록해 재계획을 받는다"
        )
    if command and APPROVED_FLAG in command.split() and not any_approved(ledger):
        return (
            "승인된 단계가 없어 --approved 를 쓸 수 없다. 결제·발송·삭제는 소유자가 다음 말로 승인한 뒤 "
            "approve 를 기록하고 진행한다. 지금은 멈추고 승인을 구한다"
        )
    step = waiting_approval(ledger)
    if step is None or tool in READ_TOOLS:
        return ""
    if (env.get("TURN_ID") or "").strip() and env.get("TURN_ID") != ledger.get("turn"):
        # 2026-11-28 두 턴 시험 : 승인 턴에서 approve 전에 executor 를 불러, executor 가
        # 장부 거부를 받고 빈손으로 돌아왔다. 승인 기록이 먼저다.
        if tool == "Agent" and tool_input.get("subagent_type") == "executor":
            return (
                f"{step['n']}단계 '{step['do']}' 의 승인이 아직 장부에 없다. 소유자의 이번 말이 승인이면 "
                "장부 명령 approve 를 먼저 부르고 '승인 기록' 출력을 확인한 뒤 executor 를 부른다. "
                "승인이 아니면 executor 를 부르지 않는다"
            )
        return ""
    return (
        f"{step['n']}단계 '{step['do']}' 는 소유자 승인이 필요하다. 이 턴에서는 더 진행하지 않는다. "
        "지금까지 한 단계와 승인할 내용을 적어 답하고 답 끝에 [[ASK]] 를 붙인다"
    )


def gate_main() -> int:
    with contextlib.suppress(Exception):
        if os.environ.get("SCA_PLAN_NESTED") or os.environ.get("SCA_VERIFY_NESTED"):
            return 0
        data = json.load(sys.stdin)
        reason = gate(data)
        if reason:
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                },
            }, ensure_ascii=False))
    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "--gate":
        return gate_main()
    if len(argv) >= 3 and argv[1] == "--ledger":
        code, out = run_command(Path(argv[2]), argv[3:])
        print(out)
        return code
    print("사용법 : plan_steps.py --ledger <경로> show|start N|done N [메모]|fail N 사유|approve|cancel")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
