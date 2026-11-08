"""PreToolUse guard: stop the same work being repeated response after response.

serial_read_guard.py stops a run of *different* one-at-a-time lookups. This
hook stops a run of the *same* call: within the current request (everything
after the last human input), when each of the previous ``RUN`` responses
(default 3, REPEAT_GUARD_RUN) made a call essentially the same as this one,
this call is denied with a reason telling the model to change approach --
put the variations into one script, rethink a failure that keeps repeating,
or wait inside one command instead of polling turn by turn.

"Essentially the same" is judged per call, by ``similar()``:

- same tool name, and
- the same normalized input, or one whose difflib ratio is at least
  ``SIMILARITY`` (default 0.9, REPEAT_GUARD_SIMILARITY). For Bash only the
  command counts (whitespace collapsed; description and timeout ignored).
  1.0 means exact matches only.
- A paging key (offset, page, cursor, ...) with a different value makes two
  calls different: reading a file page by page is serial reading, which is
  serial_read_guard's job, not repetition.

A prior response counts towards the run when any of its calls is similar to
this one, so a polled command stays caught when it rides in a small batch.

Escape hatch: a response this guard denied breaks the run, so repeating the
call right after a deny is allowed, and a call the model really has to
repeat is denied at most once every ``RUN + 1`` responses.

Shares the transcript parsing of serial_read_guard (same request boundary
rule, same tool_use grouping by message id). Like that hook it imports from
the slack_cli_agent package, so the engine runs it with its own interpreter
(sys.executable); when the package is not importable the src root next to
this file is put on sys.path first.

Never fails the tool call -- every path ends in exit 0. A denial is
communicated through the PreToolUse JSON on stdout. No sibling wait: the
decision does not depend on the current response's other calls.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

try:
    from slack_cli_agent.guard.pretool.serial_read_guard import (
        Response,
        _env_number,
        _read_records,
        responses_since_prompt,
    )
except ImportError:  # pragma: no cover - run straight from a source checkout
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from slack_cli_agent.guard.pretool.serial_read_guard import (
        Response,
        _env_number,
        _read_records,
        responses_since_prompt,
    )

#: Absolute path to this file, for the engine adapter that builds the hook
#: command (same pattern as the other guard/pretool modules' HOOK_SCRIPT).
HOOK_SCRIPT = Path(__file__).resolve()

#: Prior responses with an essentially-same call before the next one is
#: denied. 3 means the fourth identical round trip is stopped.
DEFAULT_RUN = 3

#: Minimum difflib ratio between two normalized inputs of the same tool.
DEFAULT_SIMILARITY = 0.9

#: Tools whose calls repeat by design and cost nothing to repeat.
EXEMPT_TOOLS = frozenset({"TodoWrite"})

#: Input keys whose differing values mean "the next page", not a repeat.
PAGING_KEYS = ("offset", "page", "cursor", "start", "start_line", "page_token")

#: After the common prefix and suffix are taken off, middles longer than this
#: are not compared: SequenceMatcher is quadratic in the worst case (0.4s on
#: 8000 repetitive chars) and the hook must stay cheap.
MAX_FUZZY_CHARS = 2000

#: Start of every denial reason. The denied call's tool_result carries this
#: text, which is how the next call recognises "this was just denied".
MARKER = "[같은 작업 반복]"

REASON = (
    MARKER + " 이번 요청에서 거의 같은 호출을 {run}번 연달아 불렀다. "
    "같은 호출을 또 부르지 말고 방법을 바꿔라. "
    "값을 하나씩 바꿔 보는 중이면 스크립트 하나에 묶어 한 번에 돌리고, "
    "같은 실패가 되풀이되면 원인부터 다시 본다. "
    "끝나기를 기다리는 중이면 한 명령 안에서 끝날 때까지 기다린다."
)

_SPACE = re.compile(r"\s+")


def normalize(name: str, tool_input: dict[str, Any]) -> str:
    """The text two calls are compared on."""
    if name == "Bash":
        return _SPACE.sub(" ", str(tool_input.get("command", ""))).strip()
    kept = {k: v for k, v in tool_input.items() if k != "description"}
    return json.dumps(kept, ensure_ascii=False, sort_keys=True, default=str)


def similar(
    a_name: str, a_input: dict[str, Any], b_name: str, b_input: dict[str, Any],
    threshold: float = DEFAULT_SIMILARITY,
) -> bool:
    if a_name != b_name:
        return False
    if any(a_input.get(k) != b_input.get(k) for k in PAGING_KEYS):
        return False
    a, b = normalize(a_name, a_input), normalize(b_name, b_input)
    if a == b:
        return True
    if threshold >= 1:
        return False
    return ratio_at_least(a, b, threshold)


def ratio_at_least(a: str, b: str, threshold: float) -> bool:
    """difflib-style ratio (2 * matches / total length) of two strings, with
    the common prefix and suffix counted as matches up front, so a long
    script changed in one spot compares in microseconds."""
    total = len(a) + len(b)
    prefix = len(os.path.commonprefix([a, b]))
    a_rest, b_rest = a[prefix:], b[prefix:]
    suffix = len(os.path.commonprefix([a_rest[::-1], b_rest[::-1]]))
    a_mid = a_rest[: len(a_rest) - suffix]
    b_mid = b_rest[: len(b_rest) - suffix]
    fixed = prefix + suffix
    if 2 * (fixed + min(len(a_mid), len(b_mid))) < threshold * total:
        return False
    if not a_mid or not b_mid:
        return 2 * fixed >= threshold * total
    if max(len(a_mid), len(b_mid)) > MAX_FUZZY_CHARS:
        return False
    matcher = difflib.SequenceMatcher(None, a_mid, b_mid, autojunk=False)
    needed = threshold * total / 2 - fixed
    return matcher.quick_ratio() * (len(a_mid) + len(b_mid)) / 2 >= needed and (
        matcher.ratio() * (len(a_mid) + len(b_mid)) / 2 >= needed
    )


def judge(
    responses: list[Response],
    tool_use_id: str | None,
    name: str,
    tool_input: dict[str, Any],
    run_needed: int = DEFAULT_RUN,
    threshold: float = DEFAULT_SIMILARITY,
) -> tuple[str, int]:
    """Decide one call. Returns (verdict, run) where verdict is one of
    ``deny``, ``exempt``, ``short_run``.

    ``responses`` must be parsed with this guard's MARKER so its own denies
    are marked. Pure function over the parsed transcript so the offline
    replay uses the exact same rule as the live hook.
    """
    if name in EXEMPT_TOOLS:
        return "exempt", 0
    prior = responses
    for index, response in enumerate(responses):
        if tool_use_id and any(u.tool_use_id == tool_use_id for u in response.uses):
            prior = responses[:index]
            break
    else:
        if not tool_use_id and responses and any(
            u.name == name and u.tool_input == tool_input for u in responses[-1].uses
        ):
            # Older payloads carry no tool_use_id: take a matching last
            # response as this call's own so it is not counted against itself.
            prior = responses[:-1]
    run = 0
    for response in reversed(prior):
        if any(u.denied for u in response.uses):
            break
        if not any(similar(u.name, u.tool_input, name, tool_input, threshold) for u in response.uses):
            break
        run += 1
        if run >= run_needed:
            break
    return ("deny" if run >= run_needed else "short_run"), run


def decide(data: dict[str, Any]) -> tuple[str, int]:
    raw_path = data.get("transcript_path")
    if not raw_path or not os.path.exists(raw_path):
        return "no_transcript", 0
    run_needed = int(_env_number("REPEAT_GUARD_RUN", DEFAULT_RUN))
    if run_needed <= 0:
        return "off", 0
    threshold = _env_number("REPEAT_GUARD_SIMILARITY", DEFAULT_SIMILARITY)
    responses = responses_since_prompt(_read_records(Path(raw_path)), marker=MARKER)
    return judge(
        responses, data.get("tool_use_id"), str(data.get("tool_name") or ""),
        data.get("tool_input") or {}, run_needed, threshold,
    )


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
