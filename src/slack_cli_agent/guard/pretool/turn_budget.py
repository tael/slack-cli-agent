"""PreToolUse guard: one Bash call must not eat the whole turn.

Ports the turn-budget judgment from mametchi's turn_budget_hook.py
(read-only reference at ~/.mametchi/turn_budget_hook.py) as an engine
built-in, so a bot built from this repo gets the same protection with zero
profile config: the engine propagates the wall-clock time a turn is cut off
at as TURN_DEADLINE (and, when available, the sleep-proof monotonic
equivalent as TURN_DEADLINE_MONO -- engine/environment.py's
ADDITIONAL_ALLOWED_VARS already allowlists both), and this hook denies a
Bash call whose own `timeout` would run past what is left once a wrap-up
margin is set aside. Whatever the Bash call is stuck on, it cannot eat a
turn that never gets to send an answer.

A call with no `timeout` is read as Claude Code's own default of 120s.
`run_in_background` calls do not hold the turn open, so they are exempt.

Differences from the mametchi original:
- Env var names are this repo's organization-neutral TURN_DEADLINE /
  TURN_DEADLINE_MONO, not mametchi's MAMETCHI_TURN_DEADLINE[_MONO] (same
  reasoning as engine/environment.py's allowlist comment).
- The judgment log path is opt-in (argv[1] or the TURN_BUDGET_LOG env var)
  instead of a hardcoded ~/.mametchi/logs path, and is silently skipped
  when neither is given -- this repo has no state-dir convention of its
  own yet (see guard/finish_review.py's module docstring for the same
  point about its own counter file).

Standalone and import-free beyond the standard library, for the same
reason observability/progress_hook.py is: invoked as
``<python> <this file> [log path]`` by the engine's own subprocess, whose
environment is a deliberately minimal allowlist.

Never fails the tool call -- every path here ends in exit 0. A denial is
communicated through the PreToolUse JSON on stdout, not a nonzero exit.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path

#: Absolute path to this file, for the engine adapter that builds the hook
#: command (same pattern as observability/progress_hook.py's HOOK_SCRIPT).
HOOK_SCRIPT = Path(__file__).resolve()

#: Claude Code's own default Bash timeout when a call doesn't pass one.
DEFAULT_BASH_MS = 120_000

#: Seconds reserved at the end of the turn for the model to read the Bash
#: result and write/send an answer, after the Bash call itself finishes.
DEFAULT_WRAPUP_SEC = 60


def _log_path(argv: list[str]) -> Path | None:
    if len(argv) >= 2 and argv[1]:
        return Path(argv[1])
    env_path = os.environ.get("TURN_BUDGET_LOG")
    return Path(env_path) if env_path else None


def _log(path: Path | None, rec: dict[str, object]) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        return


def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }, ensure_ascii=False))


def _remaining_seconds(env: Mapping[str, str], wrapup: int) -> float | None:
    """Seconds left in the turn once wrap-up is set aside, or None when
    neither deadline env var is set (not a bot turn -- a human session)."""
    mono_deadline = env.get("TURN_DEADLINE_MONO")
    if mono_deadline:
        return float(mono_deadline) - time.monotonic() - wrapup
    wall_deadline = env.get("TURN_DEADLINE")
    if wall_deadline:
        return float(wall_deadline) - time.time() - wrapup
    return None


def main(argv: list[str]) -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 - never fail the tool call
        return 0
    try:
        if data.get("tool_name") != "Bash":
            return 0
        env = os.environ
        tool_input = data.get("tool_input") or {}
        if tool_input.get("run_in_background"):
            return 0
        wrapup = int(env.get("TURN_WRAPUP_SEC", str(DEFAULT_WRAPUP_SEC)))
        remaining = _remaining_seconds(env, wrapup)
        if remaining is None:
            return 0
        wanted = tool_input.get("timeout")
        wanted_ms = wanted if isinstance(wanted, (int, float)) else DEFAULT_BASH_MS
        ok = wanted_ms / 1000 <= remaining
        _log(_log_path(argv), {
            "at": int(time.time()), "session": data.get("session_id"),
            "want_s": round(wanted_ms / 1000), "remaining_s": round(remaining),
            "ok": ok, "cmd": str(tool_input.get("command") or "")[:120],
        })
        if ok:
            return 0
        left = max(int(remaining), 0)
        deny(
            f"이 턴은 답을 마무리할 시간을 빼면 {left}초 남았다. 이 Bash 는 timeout 이 "
            f"{int(wanted_ms / 1000)}초라 넘는다. timeout 을 {left}초 이하로 주거나, 오래 걸릴 일은 "
            "run_in_background 로 돌리고 WATCH 로 넘긴다. 입력을 기다릴 수 있는 명령은 < /dev/null 을 붙인다."
        )
    except Exception:  # noqa: BLE001 - never fail the tool call
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
