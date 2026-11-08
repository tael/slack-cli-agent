"""PostToolUse hook: break a run of one-at-a-time lookups inside a turn.

Ports mametchi's parallel_nudge_hook.py (read-only reference at
~/.mametchi/parallel_nudge_hook.py) as an engine built-in. The rule "batch
independent lookups into one response" sits in the instructions already, but
as one line among hundreds the model does not recall it mid-investigation, so
the reminder is repeated at the moment it applies.

Behaviour
- After every tool call, read the tail of the transcript and group this
  request's assistant responses (one message id = one response).
- When NUDGE_EVERY responses in a row each made a single read-only call, add
  an ``additionalContext`` reminder. It repeats every NUDGE_EVERY more.
- Never blocks: some lookups really do need the previous result to decide the
  next one.

Differences from the mametchi original: organization-neutral wording, and
the Bash write pattern keeps only generic write verbs (no org-specific script
names).

Standalone and import-free beyond the standard library, for the same reason
guard/pretool/turn_budget.py is. Never fails the tool call -- every path ends
in exit 0.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

#: Absolute path to this file, for the engine adapter that builds the hook
#: command (same pattern as guard/pretool/turn_budget.py's HOOK_SCRIPT).
HOOK_SCRIPT = Path(__file__).resolve()

NUDGE_EVERY = 3
TAIL_BYTES = 600_000

READ_TOOLS = frozenset({"Read", "Grep", "Glob", "WebFetch", "WebSearch", "ToolSearch"})
MCP_READ = re.compile(
    r"(query|search|get|list|read|fetch|history|replies|permalink|status)", re.IGNORECASE,
)
BASH_WRITE = re.compile(
    r"(restart|\brm\s|\bmv\s|git (commit|push)|-X\s*(POST|PUT|DELETE|PATCH)|>\s*[^&/]"
    # Added 2026-11-08 when guard/pretool/serial_read_guard.py started denying
    # on this judgment: signals, copies, in-place edits and scripted writes
    # are not lookups.
    r"|\bkill\s|\bpkill\s|\bcp\s|\bsed\s+-i|\btee\s|\bmkdir\s|\btouch\s|\bchmod\s"
    r"|\blaunchctl\s|open\([^)]*['\"][wa]b?['\"]|\.write_text\(|\.write\()"
)

MESSAGE = (
    "[병렬 점검] 조회를 하나씩 {run}번 연달아 불렀다. 응답 하나마다 왕복 시간이 더해진다. "
    "남은 조회를 지금 전부 적고, 앞 결과 없이도 부를 수 있는 것은 다음 응답에서 한꺼번에 부른다. "
    "같은 파일을 sed, grep 으로 조각내 읽는 중이면 Read 로 필요한 범위를 한 번에 읽는다. "
    "파일 여러 개, 로그와 설정, 여러 집계는 한 응답에 묶는다. 갈래가 셋 이상이면 Agent 를 한 응답에서 함께 띄운다. "
    "앞 결과가 있어야 다음을 정할 수 있는 조회만 순서대로 둔다."
)

Step = tuple[Any, list[tuple[str, dict[str, Any]]]]


def is_read(name: str, tool_input: dict[str, Any]) -> bool:
    if name in READ_TOOLS:
        return True
    if name.startswith("mcp__"):
        return bool(MCP_READ.search(name.split("__")[-1]))
    if name == "Bash":
        return not BASH_WRITE.search(str(tool_input.get("command", "")))
    return False


def _is_human_turn(content: object) -> bool:
    """A user line that is a person's request, not just tool results."""
    if isinstance(content, str):
        return True
    return isinstance(content, list) and not any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


def steps_since_prompt(path: Path) -> list[Step]:
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > TAIL_BYTES:
            handle.seek(size - TAIL_BYTES)
            handle.readline()
        lines = handle.read().decode("utf-8", "ignore").splitlines()
    steps: list[Step] = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        kind = record.get("type")
        message = record.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        if kind == "user":
            if _is_human_turn(content):
                steps = []
            continue
        if kind != "assistant" or not isinstance(content, list):
            continue
        uses = [
            (str(b.get("name", "")), b.get("input") or {})
            for b in content
            if isinstance(b, dict) and b.get("type") == "tool_use"
        ]
        if not uses:
            continue
        message_id = message.get("id")
        if steps and steps[-1][0] == message_id:
            steps[-1][1].extend(uses)
        else:
            steps.append((message_id, uses))
    return steps


def single_read_run(steps: list[Step]) -> int:
    """How many responses at the end each made exactly one read-only call."""
    run = 0
    for _, uses in reversed(steps):
        if len(uses) == 1 and is_read(*uses[0]):
            run += 1
        else:
            break
    return run


def main(argv: list[str]) -> int:
    try:
        data = json.load(sys.stdin)
        raw_path = data.get("transcript_path")
        if not raw_path or not os.path.exists(raw_path):
            return 0
        run = single_read_run(steps_since_prompt(Path(raw_path)))
    except Exception:  # noqa: BLE001 - never fail the tool call
        return 0
    if run == 0 or run % NUDGE_EVERY:
        return 0
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": MESSAGE.format(run=run),
        },
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
