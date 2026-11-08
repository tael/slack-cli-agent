"""PreToolUse Bash guard: refuse in-turn polling loops that sleep while waiting.

The instructions already say "do not poll a long external job with a sleep
loop inside the turn; check once and hand it to WATCH". In a measured thread
(2026-11-08, 2-region weather report) three foreground calls still waited in
``for ...: time.sleep(1)`` loops for a background fetch or a log line, taking
100s, 49s and 42s of one 426s request. A reminder does not stop that, so this
guard denies the call itself.

Behaviour
- Deny a foreground Bash command that sleeps inside a loop: a shell
  ``while``/``until``/``for`` loop whose body runs ``sleep``, or Python
  ``time.sleep``/``sleep`` inside a ``for``/``while`` loop.
- Exempt ``run_in_background`` calls and commands detached with ``nohup`` --
  they do not hold the turn open.
- A single ``sleep N`` with no loop is left alone, and so is a short counted
  loop (fewer than POLL_MIN_ITER iterations), which is a retry with backoff.
- A loop inside a heredoc that ``cat``/``tee`` only writes to a file is not
  run by the command, so it is ignored.
- Optional jsonl log of denials (argv[1] or SLEEP_POLL_GUARD_LOG).

Standalone and import-free beyond the standard library, like the other
guard/pretool scripts. Never fails the tool call on its own error.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

#: Absolute path to this file, for the engine adapter that builds the hook
#: command (same pattern as observability/progress_hook.py's HOOK_SCRIPT).
HOOK_SCRIPT = Path(__file__).resolve()

#: Shell wait loops: while/until with a sleep in the body.
SHELL_WAIT_LOOP = re.compile(r"\b(while|until)\b[^\n]*?(;|\n)\s*do\b.*?\bsleep\s+\d", re.DOTALL)
#: Shell for-loops over a long counted range (seq N, N >= POLL_MIN_ITER) that sleep.
SHELL_SEQ_LOOP = re.compile(r"\bfor\b[^\n]*\$\(seq\s+(?:\d+\s+)?(\d+)\)[^\n]*?(;|\n)\s*do\b.*?\bsleep\s+\d", re.DOTALL)
PY_SLEEP = re.compile(r"\b(time\.)?sleep\s*\(")
PY_WHILE = re.compile(r"(^|[\s;:])while\b", re.MULTILINE)
PY_RANGE = re.compile(r"\bfor\s+\w+\s+in\s+range\(\s*(?:\d+\s*,\s*)?(\d+)")
#: A short counted loop with a sleep is a retry with backoff, not a wait; the
#: measured waits used range(90), range(270) and range(280).
POLL_MIN_ITER = 20
#: Heredoc whose body only gets written to a file (cat > f <<EOF, tee f <<EOF):
#: the loop inside is not run by this command.
FILE_HEREDOC = re.compile(
    r"^[^\n]*\b(cat|tee)\b[^\n]*<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n.*?^\s*\2\s*$",
    re.DOTALL | re.MULTILINE,
)
DETACHED = re.compile(r"\bnohup\b")

REASON = (
    "[대기 루프 차단] 턴 안에서 sleep 루프로 다른 작업이 끝나기를 기다리는 명령은 막는다. "
    "기다리는 동안 요청 처리 시간이 그대로 늘어난다. 지금 상태를 한 번만 조회해 답에 쓰고, "
    "끝난 뒤 확인할 일은 답 끝에 WATCH 로 넘긴다. 정말 오래 돌릴 작업이면 run_in_background 로 띄운다."
)


def is_sleep_poll(command: str) -> bool:
    if DETACHED.search(command):
        return False
    command = FILE_HEREDOC.sub("", command)
    if SHELL_WAIT_LOOP.search(command):
        return True
    for match in SHELL_SEQ_LOOP.finditer(command):
        if int(match.group(1)) >= POLL_MIN_ITER:
            return True
    if not PY_SLEEP.search(command):
        return False
    if PY_WHILE.search(command):
        return True
    return any(int(n) >= POLL_MIN_ITER for n in PY_RANGE.findall(command))


def _log(argv: list[str], record: dict[str, Any]) -> None:
    raw = argv[1] if len(argv) >= 2 and argv[1] else os.environ.get("SLEEP_POLL_GUARD_LOG")
    if not raw:
        return
    try:
        path = Path(raw)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def main(argv: list[str]) -> int:
    try:
        data = json.load(sys.stdin)
        if data.get("tool_name") != "Bash":
            return 0
        tool_input = data.get("tool_input") or {}
        if tool_input.get("run_in_background"):
            return 0
        command = str(tool_input.get("command", ""))
        if not is_sleep_poll(command):
            return 0
    except Exception:  # noqa: BLE001 - never fail the tool call
        return 0
    _log(argv, {
        "t": time.strftime("%Y-%m-%d %H:%M:%S"),
        "session": data.get("session_id"),
        "command": command[:300],
    })
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": REASON,
        },
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
