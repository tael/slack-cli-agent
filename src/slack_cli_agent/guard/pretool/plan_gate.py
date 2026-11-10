"""PreToolUse guard: ask for a `[계획]` block before the third tool-calling response.

The default flow (prompt FLOW_NOTE, FlowSection) has the model sort each
request into a short answer, independent branches or an exploration, and
write a `[계획]` block with the first tool call of a branching or exploring
request. 2026-11-06..10 guchipachi: of 43 sessions with 30+ tool calls, 3
used a lookup subagent; the plan step that names branches is what was
missing. A short answer ends within two tool-calling responses, so it never
meets this hook.

Rule, within the current request (everything after the last human input):

- when the current call belongs to the ``N``-th (default 3, env PLAN_GATE)
  tool-calling response or later, and no assistant text of this request so
  far contains ``[계획]`` (``[계획 갱신]`` counts too), the call is denied
  with a reason that gives the block format.

Escape hatches:

- Text of the current response counts: Claude Code writes a response's text
  block before its tool_use lines, so a response that opens with the plan
  passes on its own.
- Repeating a denied call after writing a plan passes by the rule above.
- After ``MAX_DENIES`` denied responses in one request the hook stops asking, so a
  model that keeps ignoring it loses at most that many round trips.

On only where the engine set PLAN_GATE (core/pipeline._plan_gate_env, the
same flow_applies as FlowSection). Absent, empty or <= 0 means off.

Transcript parsing follows serial_read_guard (grouping by message.id), but
text-only lines are kept because the plan is text, and a meta user line
(Stop hook feedback) does not start a new request (is_request_start). Standard library only on the hot path.

Never fails the tool call -- every path ends in exit 0. A denial is
communicated through the PreToolUse JSON on stdout.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Absolute path to this file, for the engine adapter that builds the hook
#: command (same pattern as the other guard/pretool modules' HOOK_SCRIPT).
HOOK_SCRIPT = Path(__file__).resolve()

#: Env var the pipeline sets on a turn the flow applies to. Its value is the
#: response number from which a call needs a plan first.
PLAN_GATE_ENV = "PLAN_GATE"

#: Default for PLAN_GATE when the pipeline sets it (RuntimeSettings
#: plan_gate_responses). Two tool-calling responses cover a short answer.
DEFAULT_RESPONSES = 3

#: Denied responses per request before the hook gives up asking. Counted
#: per response, not per call: a denied parallel batch is one round trip.
MAX_DENIES = 2

#: What counts as a plan in assistant text. `[계획 갱신]` contains neither
#: exactly, so both are listed.
PLAN_TAGS = ("[계획]", "[계획 갱신]")

TAIL_BYTES = 600_000

#: Start of every denial reason. The denied call's tool_result carries this
#: text, which is how later calls count denials in this request.
MARKER = "[계획 없음]"

REASON = (
    MARKER + " 이번 요청에서 도구를 부른 응답이 {count}번째인데 앞서 `[계획]` 블록이 없다. "
    "이 응답 본문에 아래 형식으로 계획을 적고 같은 호출을 다시 불러라.\n"
    "[계획]\n"
    "1. 목표 : <알아낼 것> / 원천 : <테이블, 파일, API> / 의존 : 없음 / 반환 : <수치, 목록, 판정>\n"
    "2. 목표 : ... / 원천 : ... / 의존 : 1 / 반환 : ...\n"
    "갈래는 2-5개다. 탐색형이면 첫 갈래 1-2개만 적고 결과를 본 뒤 `[계획 갱신]`으로 잇는다. "
    "의존이 없는 갈래는 한 응답에서 함께 부르고, 도구 호출 3회 이상이 드는 갈래는 "
    "Agent(subagent_type=\"lookup\") 또는 Agent(subagent_type=\"analyst\")에 "
    "범위, 목표, 금지 조건, 반환 형식 네 항목으로 맡긴다."
)


@dataclass
class Response:
    """One assistant message of this request: its text and its tool uses."""

    message_id: Any
    text: str = ""
    tool_use_ids: list[str] = field(default_factory=list)
    denied: int = 0

    @property
    def calls_tools(self) -> bool:
        return bool(self.tool_use_ids)

    @property
    def has_plan(self) -> bool:
        return any(tag in self.text for tag in PLAN_TAGS)


def _is_human_turn(content: object) -> bool:
    """A user line that is a person's request, not just tool results.
    Same rule as serial_read_guard._is_human_turn."""
    if isinstance(content, str):
        return True
    return isinstance(content, list) and not any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


def is_request_start(record: dict[str, Any]) -> bool:
    """A user line that starts a new request. Unlike serial_read_guard, a
    meta line (``isMeta``: Stop hook feedback, a finish-review block) is a
    continuation of the same request, so a plan written before the block
    still counts after it."""
    if record.get("type") != "user" or record.get("isMeta"):
        return False
    message = record.get("message") or {}
    content = message.get("content") if isinstance(message, dict) else None
    return _is_human_turn(content)


def _result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return ""


def responses_since_prompt(records: list[dict[str, Any]]) -> list[Response]:
    """This request's assistant messages grouped by message id, text-only ones
    included, with each response's count of uses this guard denied."""
    responses: list[Response] = []
    owner: dict[str, Response] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        kind = record.get("type")
        message = record.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        if kind == "user":
            if is_request_start(record):
                responses = []
                owner = {}
                continue
            for block in content or []:
                if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                    continue
                response = owner.get(str(block.get("tool_use_id")))
                if response is not None and _result_text(block).lstrip().startswith(MARKER):
                    response.denied += 1
            continue
        if kind != "assistant" or not isinstance(content, list):
            continue
        message_id = message.get("id")
        if responses and message_id is not None and responses[-1].message_id == message_id:
            response = responses[-1]
        else:
            response = Response(message_id)
            responses.append(response)
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                response.text += str(block.get("text") or "") + "\n"
            elif block.get("type") == "tool_use":
                use_id = str(block.get("id", ""))
                response.tool_use_ids.append(use_id)
                owner[use_id] = response
    return [r for r in responses if r.calls_tools or r.text.strip()]


def judge(
    responses: list[Response],
    tool_use_id: str | None,
    threshold: int = DEFAULT_RESPONSES,
    max_denies: int = MAX_DENIES,
) -> tuple[str, int]:
    """Decide one call. Returns (verdict, count) where count is the call's
    tool-calling response number in this request and verdict is one of
    ``deny``, ``early``, ``planned``, ``same_response_plan``, ``gave_up``.

    Pure function over the parsed transcript so the offline replay uses the
    exact same rule as the live hook.
    """
    index: int | None = None
    if tool_use_id:
        for i, response in enumerate(responses):
            if tool_use_id in response.tool_use_ids:
                index = i
                break
    if index is not None:
        current: Response | None = responses[index]
        prior = responses[:index]
    else:
        # The call's own tool_use line may not be on record yet. A trailing
        # text-only message is then this call's own response's text: Claude
        # Code writes the text block line first, under the same message id.
        if responses and not responses[-1].calls_tools:
            current, prior = responses[-1], responses[:-1]
        else:
            current, prior = None, responses
    count = sum(1 for r in prior if r.calls_tools) + 1
    if count < threshold:
        return "early", count
    if any(r.has_plan for r in prior):
        return "planned", count
    if current is not None and current.has_plan:
        return "same_response_plan", count
    if sum(1 for r in prior if r.denied) >= max_denies:
        return "gave_up", count
    return "deny", count


def _read_records(path: Path) -> list[dict[str, Any]]:
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > TAIL_BYTES:
            handle.seek(size - TAIL_BYTES)
            handle.readline()
        lines = handle.read().decode("utf-8", "ignore").splitlines()
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def threshold_from_env(env: dict[str, str] | os._Environ[str] | None = None) -> int:
    """PLAN_GATE as an int; 0 (off) when absent, empty or not a number."""
    raw = (env if env is not None else os.environ).get(PLAN_GATE_ENV, "")
    try:
        return int(str(raw).strip() or 0)
    except ValueError:
        return 0


def decide(data: dict[str, Any]) -> tuple[str, int]:
    threshold = threshold_from_env()
    if threshold <= 0:
        return "off", 0
    raw_path = data.get("transcript_path")
    if not raw_path or not os.path.exists(raw_path):
        return "no_transcript", 0
    return judge(
        responses_since_prompt(_read_records(Path(raw_path))),
        data.get("tool_use_id"),
        threshold,
    )


def main(argv: list[str]) -> int:
    try:
        data = json.load(sys.stdin)
        verdict, count = decide(data)
    except Exception:  # noqa: BLE001 - never fail the tool call
        return 0
    if verdict != "deny":
        return 0
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": REASON.format(count=count),
        },
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
