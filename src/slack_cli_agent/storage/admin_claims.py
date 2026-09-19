"""Cross-process record of which admin commands have already been run.

Ingress and the worker are separate processes and the in-memory dedup record
does not cross between them (reliability/dedup.py). A normal request is
protected by the jobs table's UNIQUE(channel, message_ts), but an admin
command never enters the queue, so nothing stopped both processes from
running the same one (sca-8m5p).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from .database import Database
from .repository import SqliteRepository


class ClaimState(StrEnum):
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


@dataclass(frozen=True)
class StaleClaim:
    channel: str
    ts: str
    owner: str


class AdminClaims(SqliteRepository):
    def __init__(self, db: Database, now: Callable[[], float] = time.time) -> None:
        super().__init__(db)
        self._now = now

    def claim(self, channel: str, ts: str, *, owner: str) -> bool:
        """True means this process may run the command. A row in any state
        blocks: a finished command must not run twice, and a failed one may
        have applied part of its effect already."""
        with self._transaction() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO admin_claims"
                " (channel, message_ts, owner, state, claimed_at) VALUES (?, ?, ?, ?, ?)",
                (channel, ts, owner, ClaimState.RUNNING.value, self._now()),
            )
            return cursor.rowcount > 0

    def finish(self, channel: str, ts: str, *, owner: str, ok: bool, failure: str = "") -> bool:
        """False means this process no longer owns the claim -- the stale
        sweep took it. Without the owner condition a slow command would
        overwrite that verdict and the sweep would have had no effect."""
        state = ClaimState.DONE if ok else ClaimState.FAILED
        cursor = self._execute(
            "UPDATE admin_claims SET state = ?, finished_at = ?, failure = ?"
            " WHERE channel = ? AND message_ts = ? AND owner = ? AND state = ?",
            (state.value, self._now(), failure, channel, ts, owner, ClaimState.RUNNING.value),
        )
        return cursor.rowcount > 0

    def state(self, channel: str, ts: str) -> ClaimState | None:
        row = self._fetch_one(
            "SELECT state FROM admin_claims WHERE channel = ? AND message_ts = ?",
            (channel, ts),
        )
        return ClaimState(row["state"]) if row is not None else None

    def reclaim_stale(self, claimed_before: float) -> list[StaleClaim]:
        """Closes claims whose process died mid-command.

        Left RUNNING they block forever: catch-up keeps finding the message
        because no mark was ever posted, and the claim stops anyone from
        handling it. Closed as FAILED rather than reopened -- the command may
        have applied part of its effect before the process died.
        """
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT channel, message_ts, owner FROM admin_claims"
                " WHERE state = ? AND claimed_at < ?",
                (ClaimState.RUNNING.value, claimed_before),
            ).fetchall()
            if not rows:
                return []
            conn.execute(
                "UPDATE admin_claims SET state = ?, finished_at = ?, failure = ?"
                " WHERE state = ? AND claimed_at < ?",
                (
                    ClaimState.FAILED.value,
                    self._now(),
                    "처리 중 프로세스가 끊겼다",
                    ClaimState.RUNNING.value,
                    claimed_before,
                ),
            )
        return [
            StaleClaim(channel=r["channel"], ts=r["message_ts"], owner=r["owner"]) for r in rows
        ]

    def purge(self, finished_before: float, *, failures_before: float) -> int:
        """Drops finished claims. The two states get different deadlines.

        A DONE row only has to outlast the catch-up window: its message
        carries the done mark, so catch-up stops finding it. A FAILED row
        carries `x`, which catch-up does not read as done
        (slack/reactions.py DONE_EMOJI), so that message stays findable
        until it falls out of the window by age -- delete the row before
        then and the command runs again. Kept far longer for that reason,
        but not forever: the table would grow without bound (codex review).
        """
        cursor = self._execute(
            "DELETE FROM admin_claims WHERE (state = ? AND finished_at < ?)"
            " OR (state = ? AND finished_at < ?)",
            (ClaimState.DONE.value, finished_before, ClaimState.FAILED.value, failures_before),
        )
        return cursor.rowcount
