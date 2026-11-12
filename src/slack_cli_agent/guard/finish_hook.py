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

import contextlib
import json
import os
import re
import sys
import time
from dataclasses import replace
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from slack_cli_agent.guard.finish_review import (
    ModelClaimVerifier,
    decide,
    draft_key,
    needs_verification,
    read_knowledge_file,
    resolve_advisor_channel,
    resolve_code_first_channel,
    resolve_verify_max_blocks,
    resolve_verify_settings,
    rows_since_last_human_input,
    time_left,
    turn_from_transcript_rows,
    verify_claim_keys,
)
from slack_cli_agent.core.markers import missing_recipient_prompt
from slack_cli_agent.guard.figurative import Precompute

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


def _flagged_path(state_dir: Path, key: str) -> Path:
    return state_dir / f"finish-hook-{key}.flagged.json"


def _drafts_path(state_dir: Path, key: str) -> Path:
    return state_dir / f"finish-hook-{key}.drafts.json"


def _read_drafts(path: Path | None) -> tuple[str, ...]:
    if path is None:
        return ()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - missing or bad file means no earlier draft
        return ()
    return tuple(str(v) for v in value) if isinstance(value, list) else ()


def _write_drafts(path: Path, drafts: tuple[str, ...]) -> None:
    with contextlib.suppress(OSError):
        path.write_text(json.dumps(list(drafts), ensure_ascii=False), encoding="utf-8")


def _read_flagged(path: Path | None) -> frozenset[str]:
    if path is None:
        return frozenset()
    try:
        return frozenset(json.loads(path.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001 - missing or bad file means nothing flagged yet
        return frozenset()


def _write_flagged(path: Path, keys: frozenset[str]) -> None:
    try:
        path.write_text(json.dumps(sorted(keys), ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


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


def _is_recipient_retry_turn(rows: Sequence[Mapping[str, Any]]) -> bool:
    """True when the latest non-meta text input is the missing-recipient
    reprompt. That rerun runs with every tool forbidden and only its tag is
    used (pipeline._retry_recipient_tag), so a claim check there has no
    evidence to read and blocks the whole body; the model then answered
    empty and the reply was lost (2026-11-09 private-taeil 20:31)."""
    head = missing_recipient_prompt().split("\n", 1)[0][:40]
    for entry in reversed(rows):
        if entry.get("type") != "user" or entry.get("isMeta"):
            continue
        message = entry.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if isinstance(content, list):
            texts = [b.get("text") for b in content if isinstance(b, Mapping) and b.get("type") == "text"]
            if not texts:
                continue
            content = str(texts[0] or "")
        if not isinstance(content, str):
            continue
        if content.lstrip().startswith("Stop hook feedback"):
            continue
        return content.lstrip().startswith(head)
    return False


def main(argv: list[str]) -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 - never block the turn through failure
        return 0
    try:
        session_id = payload.get("session_id")
        transcript_path = payload.get("transcript_path") or ""
        rows = _read_transcript_rows(transcript_path) if transcript_path else []
        if rows and _is_recipient_retry_turn(rows):
            return 0
        this_turn_rows = rows_since_last_human_input(rows) if rows else []
        turn = turn_from_transcript_rows(
            rows,
            last_assistant_message=payload.get("last_assistant_message"),
            is_advisor_channel=resolve_advisor_channel(os.environ),
            is_code_first_channel=resolve_code_first_channel(os.environ),
        )
        # The knowledge files this turn's system prompt carried, as the
        # engine runner wrote them (KNOWLEDGE_FILE_ENV). verify reads them as
        # evidence alongside the tool results.
        knowledge = read_knowledge_file(os.environ)
        if knowledge:
            turn = replace(turn, knowledge=knowledge)

        state_dir = _state_dir(payload)
        block_count = 0
        count_path: Path | None = None
        flagged_path: Path | None = None
        drafts_path: Path | None = None
        if state_dir is not None:
            key = _turn_key(session_id, this_turn_rows)
            count_path = _count_path(state_dir, key)
            flagged_path = _flagged_path(state_dir, key)
            drafts_path = _drafts_path(state_dir, key)
            block_count = _read_count(count_path)
        prior_flagged = _read_flagged(flagged_path)
        prior_drafts = _read_drafts(drafts_path)

        seconds_left = time_left(os.environ)
        verify_on, verify_model = resolve_verify_settings(os.environ)
        verifier = ModelClaimVerifier(model=verify_model) if verify_on else None
        # The figurative corrector runs on this same draft while decide()
        # (mostly the model verify) runs; see guard/figurative.Precompute.
        figurative = None
        with contextlib.suppress(Exception):
            figurative = Precompute.start(
                payload.get("last_assistant_message") or turn.answer or "", os.environ,
            )
        started = time.monotonic()
        decision = decide(
            turn, block_count=block_count, seconds_left=seconds_left, claim_verifier=verifier,
            prior_flagged=prior_flagged,
            verify_hard_max_blocks=resolve_verify_max_blocks(os.environ),
            prior_drafts=prior_drafts,
        )
        elapsed = round(time.monotonic() - started, 1)
        if figurative is not None:
            with contextlib.suppress(Exception):
                if decision.action == "block":
                    figurative.cancel()
                else:
                    figurative.finish()
        if decision.action == "block" and count_path is not None:
            _write_count(count_path, block_count + 1)
            if flagged_path is not None:
                _write_flagged(flagged_path, prior_flagged | verify_claim_keys(decision))
            if drafts_path is not None:
                _write_drafts(drafts_path, (*prior_drafts, draft_key(turn.answer)))
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
                "elapsed_sec": elapsed,
                "verify_sec": verifier.elapsed,
                "knowledge_chars": len(turn.knowledge),
                "findings": [f.detail for f in decision.findings if f.name == "verify"],
                "block_count": block_count,
            })

        result = decision.to_hook_json()
        if result:
            print(json.dumps(result, ensure_ascii=False))
    except Exception:  # noqa: BLE001 - never block the turn through failure
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
