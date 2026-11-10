"""PreToolUse guard: stop a run of one-at-a-time lookups by denying the next one.

guard/posttool/parallel_nudge.py reminds the model to batch independent
lookups after every third single-call response. Measured on 2026-11-08
(93 guchipachi sessions) the reminder was ignored: 449 of 531 tool-calling
responses made exactly one call, and runs went on to 6, 9, 12 and 18 in a
row after the reminder fired. Each response is one more model round trip
(median 4.8s), so a run of single lookups is the largest avoidable cost.

This hook turns the reminder into a decision. Within the current request
(everything after the last human input):

- when the previous ``RUN`` responses (default 6, SERIAL_READ_RUN) each made exactly one
  read-only call, and this call is read-only too, it is denied with a reason
  telling the model to put the remaining independent lookups in one response,
  or -- when it truly needs the previous result -- to repeat the same call.

Escape hatches:

- Repeating the call that was just denied (same tool, same input) is allowed.
  A denied response breaks the run, so a dependent chain is denied at most
  once every ``RUN + 1`` responses.
- A call whose response already has another tool_use on record (a parallel
  batch) is allowed. Claude Code writes the sibling tool_use lines of one
  response a second or two apart and starts running the first one before the
  rest are written, so before denying the hook re-reads the transcript for up
  to ``SIBLING_WAIT_SEC`` seconds. A batch whose siblings still are not on
  record by then gets its first call denied; that costs one round trip and is
  counted by the offline replay (tools/serial_read_replay.py).

Reads are judged by parallel_nudge.is_read so the two hooks agree on what a
lookup is.

Like guard/finish_hook.py this script imports from the slack_cli_agent
package (for is_read), so the engine runs it with its own interpreter
(sys.executable) where the package is installed. When it is not importable
the src root next to this file is put on sys.path first.

Never fails the tool call -- every path ends in exit 0. A denial is
communicated through the PreToolUse JSON on stdout.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from slack_cli_agent.guard.posttool.parallel_nudge import is_read
except ImportError:  # pragma: no cover - run straight from a source checkout
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from slack_cli_agent.guard.posttool.parallel_nudge import is_read

#: Absolute path to this file, for the engine adapter that builds the hook
#: command (same pattern as the other guard/pretool modules' HOOK_SCRIPT).
HOOK_SCRIPT = Path(__file__).resolve()

#: Single-read responses in a row before the next read is denied. The spec
#: started at 4; replaying 2026-11-08's 93 guchipachi sessions
#: (tools/serial_read_replay.py) put the share of denies whose call really
#: needed the previous result at 63% for 4, 29% for 5 (36% read by hand) and
#: 22% for 6, against a 30% ceiling. Re-run over 2026-11-06..08 (865
#: requests): 57.8% for 4, 37.2% for 5 (21 of 44 read by hand were plain
#: dependent calls or batches) and 32.0% for 6, so it stays at 6.
DEFAULT_RUN = 6

#: Seconds to keep re-reading the transcript for sibling tool_use lines of
#: the current response before denying (see module docstring).
DEFAULT_SIBLING_WAIT_SEC = 1.5
_POLL_SEC = 0.25

TAIL_BYTES = 600_000

#: Start of every denial reason. The denied call's tool_result carries this
#: text, which is how the next call recognises "this was just denied".
MARKER = "[연속 단건 조회]"

REASON = (
    MARKER + " 이번 요청에서 조회를 한 응답에 하나씩 {run}번 연달아 불렀다. "
    "남은 조사를 `[계획 갱신]`으로 갈래마다 한 줄씩 적고, 앞 결과가 필요한 갈래는 의존을 표시하라. "
    "독립 갈래가 둘 이상이면 지금 한 응답에 함께 불러라. 도구 호출 3회 이상이 드는 갈래는 "
    "Agent(subagent_type=\"lookup\")에 범위, 목표, 금지 조건, 반환 형식을 적어 맡겨라. "
    "앞 결과가 꼭 필요하면 같은 호출을 그대로 다시 불러라."
)


@dataclass
class Use:
    tool_use_id: str
    name: str
    tool_input: dict[str, Any]
    denied: bool = False
    #: The call's tool_result text and error flag once it is on record
    #: (None until then). Read by repeat_guard; this guard only needs denied.
    result: str | None = None
    is_error: bool = False


@dataclass
class Response:
    message_id: Any
    uses: list[Use] = field(default_factory=list)


def _is_human_turn(content: object) -> bool:
    """A user line that is a person's request, not just tool results.
    Same rule as parallel_nudge._is_human_turn."""
    if isinstance(content, str):
        return True
    return isinstance(content, list) and not any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


def _result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(b.get("text", "")) for b in content if isinstance(b, dict)
        )
    return ""


#: Claude Code writes a PreToolUse deny into the transcript as
#: "PreToolUse:<Tool> hook error: <reason>" (claude 2.1.321, 2026-11-10 W5),
#: so the reason does not start the tool_result. Without stripping this no
#: denial was recognised: "repeat the denied call" never passed here, and
#: repeat_guard, which parses with its own marker, never saw its denies.
_HOOK_ERROR_PREFIX = re.compile(r"^PreToolUse:\S+ hook error:\s*")


def responses_since_prompt(records: list[dict[str, Any]], marker: str = MARKER) -> list[Response]:
    """Group this request's tool-calling assistant lines by message id, and
    mark the uses a guard denied (their tool_result starts with ``marker``,
    this guard's MARKER unless another guard passes its own)."""
    responses: list[Response] = []
    by_id: dict[str, Use] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        kind = record.get("type")
        message = record.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        if kind == "user":
            if _is_human_turn(content):
                responses = []
                by_id = {}
                continue
            for block in content or []:
                if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                    continue
                use = by_id.get(str(block.get("tool_use_id")))
                if use is None:
                    continue
                use.result = _result_text(block)
                use.is_error = bool(block.get("is_error"))
                if _HOOK_ERROR_PREFIX.sub("", use.result.lstrip(), count=1).startswith(marker):
                    use.denied = True
            continue
        if kind != "assistant" or not isinstance(content, list):
            continue
        uses = [
            Use(str(b.get("id", "")), str(b.get("name", "")), b.get("input") or {})
            for b in content
            if isinstance(b, dict) and b.get("type") == "tool_use"
        ]
        if not uses:
            continue
        message_id = message.get("id")
        if responses and responses[-1].message_id == message_id:
            responses[-1].uses.extend(uses)
        else:
            responses.append(Response(message_id, uses))
        for use in uses:
            by_id[use.tool_use_id] = use
    return responses


def judge(
    responses: list[Response],
    tool_use_id: str | None,
    name: str,
    tool_input: dict[str, Any],
    run_needed: int = DEFAULT_RUN,
) -> tuple[str, int]:
    """Decide one call. Returns (verdict, run) where verdict is one of
    ``deny``, ``not_read``, ``batched``, ``retry``, ``short_run``.

    Pure function over the parsed transcript so the offline replay uses the
    exact same rule as the live hook.
    """
    if not is_read(name, tool_input):
        return "not_read", 0
    prior = responses
    current: Response | None = None
    for index, response in enumerate(responses):
        if tool_use_id and any(u.tool_use_id == tool_use_id for u in response.uses):
            current, prior = response, responses[:index]
            break
    if current is None and not tool_use_id and responses and any(
        u.name == name and u.tool_input == tool_input for u in responses[-1].uses
    ):
        # Older payloads carry no tool_use_id: take a matching last response
        # as this call's own so it is not counted towards its own run.
        current, prior = responses[-1], responses[:-1]
    if current is not None and len(current.uses) > 1:
        return "batched", 0
    if prior and any(
        u.denied and u.name == name and u.tool_input == tool_input for u in prior[-1].uses
    ):
        return "retry", 0
    run = 0
    for response in reversed(prior):
        if any(u.denied for u in response.uses):
            break
        if len(response.uses) == 1 and is_read(response.uses[0].name, response.uses[0].tool_input):
            run += 1
        else:
            break
    return ("deny" if run >= run_needed else "short_run"), run


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


def _env_number(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def decide(data: dict[str, Any]) -> tuple[str, int]:
    raw_path = data.get("transcript_path")
    if not raw_path or not os.path.exists(raw_path):
        return "no_transcript", 0
    name = str(data.get("tool_name") or "")
    tool_input = data.get("tool_input") or {}
    tool_use_id = data.get("tool_use_id")
    run_needed = int(_env_number("SERIAL_READ_RUN", DEFAULT_RUN))
    if run_needed <= 0:
        return "off", 0
    path = Path(raw_path)
    verdict, run = judge(responses_since_prompt(_read_records(path)), tool_use_id, name, tool_input, run_needed)
    deadline = time.monotonic() + _env_number("SERIAL_READ_SIBLING_WAIT_SEC", DEFAULT_SIBLING_WAIT_SEC)
    while verdict == "deny" and time.monotonic() < deadline:
        time.sleep(_POLL_SEC)
        verdict, run = judge(
            responses_since_prompt(_read_records(path)), tool_use_id, name, tool_input, run_needed,
        )
    return verdict, run


def main(argv: list[str]) -> int:
    try:
        data = json.load(sys.stdin)
        verdict, run = decide(data)
    except Exception:  # noqa: BLE001 - never fail the tool call
        return 0
    if verdict != "deny":
        return 0
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": REASON.format(run=run),
        },
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
