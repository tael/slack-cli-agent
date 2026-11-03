"""Per-thread "waiting on a human decision" state.

Ports bot.py:1513-1518, 8281-8312 (`AWAITING_FILE`/`set_awaiting`/`is_awaiting`).
A short approval ("좋아", "네", "ㅇㅇ") only reads as answering a pending
decision, not a stray reaction, when the bot's own last turn in that thread
actually asked for one. That fact has to survive a restart since the
decision can come hours later, so it's kept in the same SQLite file as
everything else that must not be lost between runs -- a session record, a
watch job.

`core.markers.ASK_RE`/`take_ask_tag` are the model's own first-line signal
that a turn is waiting; `slack.gate.ResponseGate.asked_back` is the
second-line, vocabulary-based fallback for replies that never carried the
tag. Both land here as a single boolean per thread.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from ..storage.database import Database
from ..storage.repository import SqliteRepository

#: Matches bot.py:1517's AWAITING_MAX_AGE_SEC. A question nobody answered
#: should not wait on a reply forever.
DEFAULT_AWAITING_MAX_AGE_SEC = 24 * 3600


@runtime_checkable
class AwaitingPort(Protocol):
    def set_awaiting(self, channel: str, thread_ts: str, waiting: bool) -> None:
        """Overwrites this thread's awaiting state. A turn that isn't
        waiting on a reply clears it -- the next turn's state always wins,
        same as bot.py's `set_awaiting`."""

    def is_awaiting(self, channel: str, thread_ts: str) -> bool:
        """Whether that thread is still within the max age of its last
        awaiting mark. False once the mark has aged out, even though the
        row may still physically exist until the next write."""


class SqliteAwaitingStore(SqliteRepository):
    def __init__(
        self,
        db: Database,
        *,
        max_age_sec: float = DEFAULT_AWAITING_MAX_AGE_SEC,
        now: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(db)
        self._max_age_sec = max_age_sec
        self._now = now

    def set_awaiting(self, channel: str, thread_ts: str, waiting: bool) -> None:
        if not waiting:
            self._execute(
                "DELETE FROM awaiting_replies WHERE channel = ? AND thread_ts = ?",
                (channel, thread_ts),
            )
            return
        self._execute(
            "INSERT INTO awaiting_replies (channel, thread_ts, marked_at) VALUES (?, ?, ?) "
            "ON CONFLICT(channel, thread_ts) DO UPDATE SET marked_at = excluded.marked_at",
            (channel, thread_ts, self._now()),
        )

    def is_awaiting(self, channel: str, thread_ts: str) -> bool:
        row = self._fetch_one(
            "SELECT marked_at FROM awaiting_replies WHERE channel = ? AND thread_ts = ?",
            (channel, thread_ts),
        )
        if row is None:
            return False
        return (self._now() - float(row["marked_at"])) < self._max_age_sec
