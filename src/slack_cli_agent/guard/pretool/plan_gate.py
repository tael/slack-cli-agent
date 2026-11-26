"""PreToolUse hook: inject the engine's plan once, never deny.

guard/plan_start (UserPromptSubmit) starts a tool-less planner call when a
request arrives and writes ``<transcript>.plan.json``. On the first tool call
after that plan is ready this hook adds it as ``additionalContext``, once per
request. Short plans and failed plans inject nothing, except with
SCA_PLAN_INTAKE_INJECT a short plan carrying checks or expected_output.

Until 2026-11-10 this hook denied the third tool-calling response when no
`[계획]` block came first. In W5 all 15 runs were denied twice and passed by
gave_up: Claude Code 2.1.321 writes a response's text to the transcript
50-110 ms after its PreToolUse hook starts, so even a plan written with the
call could not pass, and the 30 denials cost 3.95M tokens
(tasks/guchipachi-orchestration/plan_gate_rca.md). The plan is now made by
code and handed in, so there is nothing to deny.

On only where the engine set PLAN_GATE (core/pipeline._plan_gate_env, the
same flow_applies as FlowSection). Absent, empty or <= 0 means off. A
worker's (Agent subagent's) calls are skipped: the plan is the main's.

The delegation line and each branch's "위임" field are shown only where the
engine set AGENT_DELEGATION (the turn's tool list has Agent or Task, so the
profile's subagents exist). Absent means no delegation text: a turn without
the tool must not be told to call it.

Standard library only. Never fails the tool call -- every path ends in exit 0.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, NamedTuple

HOOK_SCRIPT = Path(__file__).resolve()

#: Env var the pipeline sets on a turn the flow applies to. Any value > 0 is on.
PLAN_GATE_ENV = "PLAN_GATE"

#: Env var the pipeline sets when this turn's tool list has Agent or Task
#: (ToolSelection.can_delegate). Absent, empty or "0" means off.
AGENT_DELEGATION_ENV = "AGENT_DELEGATION"

#: Default for PLAN_GATE when the pipeline sets it (RuntimeSettings
#: plan_gate_responses). Only its sign matters now.
DEFAULT_RESPONSES = 3

#: A plan file older than this belongs to an earlier request whose reset was
#: missed (plan_start removes it on every request).
STALE_SEC = 3600.0

#: Head of the injected text. Never shown to the person; finish_review and the
#: final answer must not mention it (see INJECT_TAIL).
INJECT_HEAD = "[작업 순서] 이 요청은 아래 갈래로 나눠 처리한다."

INJECT_TAIL_HEAD = "- 의존이 없는 갈래는 다음 응답에서 함께 부른다. 이미 끝낸 갈래는 다시 하지 않는다\n"
#: Only where AGENT_DELEGATION is set.
INJECT_TAIL_DELEGATION = (
    "- 위임이 적힌 갈래는 Agent(subagent_type=\"<위임>\")에 범위, 목표, 금지 조건, 반환 형식 "
    "네 항목으로 맡긴다\n"
)
#: Env var that turns on FORCE_PARALLEL (below). Absent, empty or "0" is off.
#: Kept behind its own switch so a replay can measure it before any turn runs it.
FORCE_PARALLEL_ENV = "SCA_PLAN_FORCE_PARALLEL"
#: Delegated branches with no dependency at or above this count get the forced
#: form. One branch has nothing to run beside it.
FORCE_PARALLEL_MIN = 2
#: Written as the calls to make, not as advice. With the advice form alone
#: ("함께 부른다", "맡긴다") H8 replay delegated in 2 of 10 runs and never ran
#: two Agent calls in one response (tasks/shinji-workflow/h8-deep-parallel,
#: 2026-11-16).
FORCE_PARALLEL_HEAD = (
    "- 아래 Agent 호출을 지금 다음 응답 하나에 모두 함께 낸다. 이 갈래들을 직접 조회로 대신하지 않는다. "
    "결과가 모두 돌아온 뒤 합쳐서 답한다\n"
)
INJECT_TAIL_END = (
    "- 갈래가 요청과 맞지 않거나 빠진 일이 있으면 요청을 따른다\n"
    "- 이 안내는 내부 작업 순서다. 답변에 이 안내, 작업 순서, 계획, 훅을 언급하지 않는다"
)
#: The tail without delegation, the default.
INJECT_TAIL = INJECT_TAIL_HEAD + INJECT_TAIL_END

#: Env var that adds expected output, assumptions and checks to the injection
#: (pipeline sets it only with SCA_PLAN_INTAKE too). Absent, empty or "0" is off.
INTAKE_INJECT_ENV = "SCA_PLAN_INTAKE_INJECT"
INJECT_HEAD_COMPACT = "[작업 기준] 이 요청은 아래 기준으로 답한다."
INJECT_ASSUMED = "- 가정을 답변에 밝혀 사용자가 고칠 수 있게 한다"
INJECT_CHECKS = "- 답변은 이 기준으로 점검된다. 확인하지 못한 기준은 답하기 전에 다른 도구, 명령, 출처로 조회해 확정한다. 모든 경로가 막힌 기준만 시도한 경로와 오류를 적는다"
#: v2 intake (SCA_PLAN_INTAKE_SCHEMA=2): input kinds, output forms and caps.
#: guard/plan_start keeps its own copies (a test pins them equal).
INPUT_KINDS = ("given", "derived", "lookup", "guess", "ask")
OUTPUT_FORMS = ("한두줄", "문단", "목록", "표", "긴글")
INPUTS_MAX = 6
OUTPUT_ITEMS_MAX = 6
KIND_LOOKUP = "lookup"
KIND_GUESS = "guess"
KIND_ASK = "ask"
INJECT_LOOKUP = (
    "- lookup 입력은 도구로 찾아 채우고 '미확인' 으로 남기지 않는다. 제품·모델·서비스·회사·앱 이름의 정체이면 "
    "볼트 지식 색인과 메모리 색인을 먼저 보고, 없으면 웹검색으로 확인한다"
)
INJECT_GUESS = (
    "- guess 값은 사용자에게 안내됐을 수 있다. 도구 결과가 다르면 바꾸고 바꾼 점을 답에 한 문장으로 밝힌다. "
    "guess 가 제품·모델·서비스·회사·앱 이름의 정체이면 답하기 전에 볼트 지식 색인과 메모리 색인을 보고, 없으면 웹검색으로 확인한다"
)
INJECT_OUTPUT = "- 답은 기대 결과의 형태와 항목대로 쓰고 그 밖의 내용은 덧붙이지 않는다"
#: Written to the once-only flag file when the intake text is on.
KIND_FULL = "full"
KIND_COMPACT = "compact"

_TYPE_LABEL = {"branch": "branch", "explore": "explore"}


def plan_path(transcript_path: str) -> Path:
    return Path(transcript_path + ".plan.json")


def injected_path(transcript_path: str) -> Path:
    return Path(transcript_path + ".plan.injected")


def threshold_from_env(env: dict[str, str] | os._Environ[str] | None = None) -> int:
    """PLAN_GATE as an int; 0 (off) when absent, empty or not a number."""
    raw = (env if env is not None else os.environ).get(PLAN_GATE_ENV, "")
    try:
        return int(str(raw).strip() or 0)
    except ValueError:
        return 0


def delegation_from_env(env: dict[str, str] | os._Environ[str] | None = None) -> bool:
    """AGENT_DELEGATION on: set and not "0". Absent or empty means off."""
    raw = (env if env is not None else os.environ).get(AGENT_DELEGATION_ENV, "")
    return str(raw).strip() not in ("", "0")


def force_parallel_from_env(env: dict[str, str] | os._Environ[str] | None = None) -> bool:
    """SCA_PLAN_FORCE_PARALLEL on: set and not "0"."""
    raw = (env if env is not None else os.environ).get(FORCE_PARALLEL_ENV, "")
    return str(raw).strip() not in ("", "0")


def intake_inject_from_env(env: dict[str, str] | os._Environ[str] | None = None) -> bool:
    """SCA_PLAN_INTAKE_INJECT on: set and not "0"."""
    raw = (env if env is not None else os.environ).get(INTAKE_INJECT_ENV, "")
    return str(raw).strip() not in ("", "0")


class IntakeView(NamedTuple):
    """One shape for v1 (assumed/missing/expected_output/checks) and v2
    (inputs/output) intake fields. ``assumed``, ``missing`` and ``checks`` keep
    the raw v1 values; for v2 the first two are derived from guess and ask
    inputs and ``checks`` is None."""

    schema: int
    expected: Any
    assumed: list[Any]
    missing: list[Any]
    checks: list[Any] | None
    inputs: list[dict[str, str]]
    output_form: str
    output_items: list[str]


def _clean_inputs(raw: Any) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        name, value, kind, reason = (item.get(key) for key in ("name", "value", "kind", "reason"))
        if not (isinstance(name, str) and name.strip() and isinstance(value, str) and kind in INPUT_KINDS):
            continue
        found.append({
            "name": name.strip(), "value": value.strip(), "kind": kind,
            "reason": reason.strip() if isinstance(reason, str) else "",
        })
    return found[:INPUTS_MAX]


def _clean_output(raw: Any) -> tuple[str, list[str]]:
    if not isinstance(raw, dict) or raw.get("form") not in OUTPUT_FORMS:
        return "", []
    items = raw.get("items")
    texts = [i.strip() for i in items if isinstance(i, str) and i.strip()] if isinstance(items, list) else []
    return raw["form"], texts[:OUTPUT_ITEMS_MAX]


def intake_view(plan: dict[str, Any]) -> IntakeView:
    if isinstance(plan.get("inputs"), list) or isinstance(plan.get("output"), dict):
        inputs = _clean_inputs(plan.get("inputs"))
        form, items = _clean_output(plan.get("output"))
        guessed = [f"{i['name']}: {i['value']}" for i in inputs if i["kind"] == KIND_GUESS]
        asked = [i["name"] for i in inputs if i["kind"] == KIND_ASK]
        return IntakeView(2, "", guessed, asked, None, inputs, form, items)
    assumed, missing, checks = plan.get("assumed"), plan.get("missing"), plan.get("checks")
    return IntakeView(
        1, plan.get("expected_output"),
        assumed if isinstance(assumed, list) else [], missing if isinstance(missing, list) else [],
        checks if isinstance(checks, list) else None, [], "", [],
    )


def _intake_parts(plan: dict[str, Any]) -> tuple[str, list[str], list[tuple[str, str]]]:
    """expected_output, assumed and checks; a malformed field or entry is dropped."""
    view = intake_view(plan)
    expected = view.expected.strip() if isinstance(view.expected, str) else ""
    assumed = [a.strip() for a in view.assumed if isinstance(a, str) and a.strip()]
    checks = []
    for item in view.checks or []:
        if not (isinstance(item, dict) and isinstance(item.get("what"), str) and isinstance(item.get("how"), str)):
            continue
        if item["what"].strip():
            checks.append((item["what"].strip(), item["how"].strip()))
    return expected, assumed, checks


def _intake_lines_v2(view: IntakeView) -> list[str]:
    lines = []
    if view.inputs:
        lines.append("입력 :")
        lines += [f"- {i['name']}: {i['value'] or '미정'} ({i['kind']})" for i in view.inputs]
    if view.output_form:
        shape = " / ".join([view.output_form, ", ".join(view.output_items)]) if view.output_items else view.output_form
        lines.append(f"기대 결과 : {shape}")
    kinds = {i["kind"] for i in view.inputs}
    if KIND_LOOKUP in kinds:
        lines.append(INJECT_LOOKUP)
    if KIND_GUESS in kinds:
        lines.append(INJECT_GUESS)
    if view.output_form:
        lines.append(INJECT_OUTPUT)
    return lines


def _intake_lines(plan: dict[str, Any]) -> list[str]:
    view = intake_view(plan)
    if view.schema == 2:
        return _intake_lines_v2(view)
    expected, assumed, checks = _intake_parts(plan)
    lines = []
    if expected:
        lines.append(f"기대 결과 : {expected}")
    if assumed:
        lines += [f"가정 : {' / '.join(assumed)}", INJECT_ASSUMED]
    if checks:
        lines.append("확인 기준 :")
        lines += [f"{i}. {what} (확인 : {how})" for i, (what, how) in enumerate(checks, start=1)]
        lines.append(INJECT_CHECKS)
    return lines


def has_compact_intake(plan: dict[str, Any]) -> bool:
    view = intake_view(plan)
    if view.schema == 2:
        return bool(view.inputs or view.output_form)
    expected, _, checks = _intake_parts(plan)
    return bool(expected or checks)


def render_compact(plan: dict[str, Any]) -> str:
    return "\n".join([INJECT_HEAD_COMPACT, *_intake_lines(plan), INJECT_TAIL_END])


def _parallel_calls(plan: dict[str, Any]) -> list[str]:
    """Agent calls for the delegated branches that depend on nothing."""
    calls = []
    for branch in plan.get("branches") or []:
        if not isinstance(branch, dict) or branch.get("depends_on"):
            continue
        delegate = branch.get("delegate") or "none"
        if delegate == "none":
            continue
        brief = (
            f"목표: {branch.get('goal', '')} / 원천: {branch.get('source', '')} / "
            f"반환: {branch.get('returns', '')}"
        )
        calls.append(f'  Agent(subagent_type="{delegate}", prompt="{brief}")')
    return calls


def render(
    plan: dict[str, Any], delegation: bool = False, force_parallel: bool = False, intake: bool = False,
) -> str:
    lines = [INJECT_HEAD, f"유형 : {_TYPE_LABEL.get(str(plan.get('type')), plan.get('type'))}"]
    done_when = [str(c) for c in plan.get("done_when") or [] if str(c).strip()]
    if done_when:
        lines.append("완료 조건 : " + " / ".join(done_when))
    for i, branch in enumerate(plan.get("branches") or [], start=1):
        if not isinstance(branch, dict):
            continue
        depends = ", ".join(str(d) for d in branch.get("depends_on") or []) or "없음"
        delegate = branch.get("delegate") or "none"
        line = (
            f"{i}. 목표 : {branch.get('goal', '')} / 원천 : {branch.get('source', '')} / "
            f"의존 : {depends} / 반환 : {branch.get('returns', '')}"
        )
        if delegation and delegate != "none":
            line += f" / 위임 : {delegate}"
        lines.append(line)
    if done_when:
        lines.append("- 답하기 전에 완료 조건을 하나씩 대조한다. 충족을 확인하지 못한 조건은 답하기 전에 다른 도구, 명령, 출처로 조회해 확정한다. 모든 경로가 막힌 조건만 시도한 경로와 오류를 적는다")
    if plan.get("type") == "explore":
        lines.append("- explore 유형이다. 첫 갈래 결과를 보고 다음 조회를 정한다")
    if intake:
        lines += _intake_lines(plan)
    tail = INJECT_TAIL_HEAD + (INJECT_TAIL_DELEGATION if delegation else "")
    calls = _parallel_calls(plan) if delegation and force_parallel else []
    if len(calls) >= FORCE_PARALLEL_MIN:
        tail += FORCE_PARALLEL_HEAD + "\n".join(calls) + "\n"
    return "\n".join(lines) + "\n" + tail + INJECT_TAIL_END


def _read_plan(path: Path, now: float) -> dict[str, Any] | None:
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(plan, dict):
        return None
    stamp = plan.get("finished_at") or plan.get("started_at") or 0
    if not isinstance(stamp, (int, float)) or now - stamp > STALE_SEC:
        return None
    return plan


def decide(data: dict[str, Any], now: float | None = None) -> tuple[str, str]:
    """(verdict, context). Only verdict ``inject`` carries context."""
    if threshold_from_env() <= 0:
        return "off", ""
    if data.get("agent_id"):
        # A worker's call. Claude Code hands its PreToolUse hook the main
        # session's transcript_path plus agent_id (claude 2.1.321, W5).
        return "subagent", ""
    transcript = data.get("transcript_path")
    if not transcript:
        return "no_transcript", ""
    plan = _read_plan(plan_path(transcript), time.time() if now is None else now)
    if plan is None:
        return "no_plan", ""
    status = plan.get("status")
    if status == "pending":
        return "pending", ""
    if status != "ok":
        return "plan_failed", ""
    intake = intake_inject_from_env()
    compact = plan.get("type") == "short" or not plan.get("branches")
    if compact and not (intake and has_compact_intake(plan)):
        return "short", ""
    try:
        # O_EXCL: parallel calls of one response run this hook at once.
        fd = os.open(injected_path(transcript), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return "already", ""
    except OSError:
        return "flag_error", ""
    try:
        if intake:
            os.write(fd, (KIND_COMPACT if compact else KIND_FULL).encode())
    except OSError:
        pass
    finally:
        os.close(fd)
    if compact:
        return "inject", render_compact(plan)
    return "inject", render(plan, delegation_from_env(), force_parallel_from_env(), intake)


def main(argv: list[str]) -> int:
    try:
        verdict, context = decide(json.load(sys.stdin))
    except Exception:  # noqa: BLE001 - never fail the tool call
        return 0
    if verdict != "inject":
        return 0
    print(json.dumps({
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": context},
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
