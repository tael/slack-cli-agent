"""Stop hook entry script: wires guard/finish_review.py's judgments into
Claude Code's Stop event.

This is the wiring finish_review.py's module docstring asks for (B4): read
the hook JSON from stdin, build a `Turn` from the transcript, resolve how
much turn budget is left, look up (and persist) how many times this same
turn has already been blocked, call `decide()`, and print the result.

Per-turn block counting mirrors mametchi's finish_work_hook.py
(count_file()): the counter file is keyed by session_id plus the first
row's `uuid` (falling back to its `timestamp`) of the rows since the last
real human message, i.e. a key that changes once a new turn starts -- so a
turn that gets released or allowed does not leave a stale block_count >= 2
behind for the next turn in the same session. The counter directory is
FINISH_HOOK_STATE_DIR when set, otherwise the transcript file's own
directory (the nearest thing to "next to wherever this session's working
files already are" without this repo inventing a state-dir convention of
its own -- see finish_review.py's docstring on that point). If neither
resolves to a writable place, block_count is just read as 0 every time:
the Stop hook still works, it only loses the "give up after two blocks"
backstop within a single turn.

Unlike the PreToolUse guards in guard/pretool/, this module imports
guard.finish_review -- allowed because, per finish_review.py's docstring,
the Stop hook runs through the bot's own sys.executable, which (unlike the
restricted *environment* claude's subprocess gets) still resolves its own
installed site-packages, slack_cli_agent included.

Never blocks a turn through failure: any exception here is swallowed and
nothing is printed, same as a `decide()` result of "allow" would do.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from slack_cli_agent.guard.finish_review import (
    ModelClaimVerifier,
    decide,
    needs_verification,
    resolve_advisor_channel,
    resolve_code_first_channel,
    resolve_verify_settings,
    rows_since_last_human_input,
    time_left,
    turn_from_transcript_rows,
)

#: Absolute path to this file, for the engine adapter that builds the hook
#: command (same pattern as observability/progress_hook.py's HOOK_SCRIPT).
HOOK_SCRIPT = Path(__file__).resolve()


def _read_transcript_rows(transcript_path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with open(transcript_path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _state_dir(payload: dict[str, Any]) -> Path | None:
    env_dir = os.environ.get("FINISH_HOOK_STATE_DIR")
    if env_dir:
        return Path(env_dir)
    transcript = payload.get("transcript_path")
    if not transcript:
        return None
    try:
        return Path(transcript).resolve().parent
    except OSError:
        return None


def _turn_key(session_id: object, this_turn_rows: Sequence[Mapping[str, object]]) -> str:
    anchor = ""
    if this_turn_rows:
        first = this_turn_rows[0]
        anchor = str(first.get("uuid") or first.get("timestamp") or "")
    anchor = re.sub(r"[^A-Za-z0-9]", "", anchor)[:40]
    sid = re.sub(r"[^A-Za-z0-9_-]", "_", str(session_id or "nosession"))
    return f"{sid}-{anchor}" if anchor else sid


def _count_path(state_dir: Path, key: str) -> Path:
    return state_dir / f"finish-hook-{key}.count"


def _read_count(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _write_count(path: Path, value: int) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(value), encoding="utf-8")
    except OSError:
        pass


def _log_verify(state_dir: Path | None, record: dict[str, Any]) -> None:
    """Appends one verify record (status, findings, decision) so a skipped
    or failed model check is visible afterwards instead of silent."""
    if state_dir is None:
        return
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        with open(state_dir / "finish-hook-verify.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def main(argv: list[str]) -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 - never block the turn through failure
        return 0
    try:
        session_id = payload.get("session_id")
        transcript_path = payload.get("transcript_path") or ""
        rows = _read_transcript_rows(transcript_path) if transcript_path else []
        this_turn_rows = rows_since_last_human_input(rows) if rows else []
        turn = turn_from_transcript_rows(
            rows,
            last_assistant_message=payload.get("last_assistant_message"),
            is_advisor_channel=resolve_advisor_channel(os.environ),
            is_code_first_channel=resolve_code_first_channel(os.environ),
        )

        state_dir = _state_dir(payload)
        block_count = 0
        count_path: Path | None = None
        if state_dir is not None:
            count_path = _count_path(state_dir, _turn_key(session_id, this_turn_rows))
            block_count = _read_count(count_path)

        seconds_left = time_left(os.environ)
        verify_on, verify_model = resolve_verify_settings(os.environ)
        verifier = ModelClaimVerifier(model=verify_model) if verify_on else None
        decision = decide(
            turn, block_count=block_count, seconds_left=seconds_left, claim_verifier=verifier
        )
        if decision.action == "block" and count_path is not None:
            _write_count(count_path, block_count + 1)
        if verifier is not None and turn.has_transcript and needs_verification(turn.answer):
            status = verifier.status
            if status == "not_run":
                status = "skip_flagged_or_budget"
            _log_verify(state_dir, {
                "t": time.strftime("%Y-%m-%d %H:%M:%S"),
                "session": str(session_id or ""),
                "verify": status,
                "model": verify_model,
                "decision": decision.action,
                "findings": [f.detail for f in decision.findings if f.name == "verify"],
            })

        result = decision.to_hook_json()
        if result:
            print(json.dumps(result, ensure_ascii=False))
    except Exception:  # noqa: BLE001 - never block the turn through failure
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
