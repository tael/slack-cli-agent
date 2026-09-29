"""Session store contract. `SessionManager` depends only on this Protocol, not
on `store.SqliteSessionStore`. Verified by tests/unit/test_session.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


class SessionScope(StrEnum):
    """Continuity unit. TTL policy (thread: 24h, channel: 7d) lives in manager."""

    THREAD = "thread"
    CHANNEL = "channel"


@dataclass(frozen=True)
class SessionKey:
    scope: str
    key: str


@dataclass(frozen=True)
class SessionRecord:
    scope: str
    key: str
    session_id: str
    engine: str
    created_at: float
    last_seen_ts: str
    updated_at: float
    workdir: str = ""
    # Runtime environment, scoped to the conversation rather than the speaker;
    # once widened it's never narrowed back. Defaults to "" so callers that
    # don't care can ignore it.
    model: str = ""


@runtime_checkable
class SessionStore(Protocol):
    def get(self, key: SessionKey) -> SessionRecord | None:
        """TTL is not this layer's concern; expired records are returned as-is."""

    def put(self, record: SessionRecord) -> None:
        ...

    def touch(self, key: SessionKey, seen_ts: str) -> None:
        """Update only the last-seen Slack timestamp, not `updated_at` (the
        TTL clock). No-op if the record doesn't exist.
        """

    def expire(self, before: float) -> int:
        """Delete records with `updated_at` before the cutoff, returning the count."""
        ...

    def reassign_session_id(
        self,
        key: SessionKey,
        expected_session_id: str,
        engine: str,
        actual_session_id: str,
        now: float,
    ) -> bool:
        """Swap in the ID the engine actually assigned.

        Only updates if the record still matches `expected_session_id` and
        `engine` — if another request already changed it, this is a no-op
        so we don't clobber that change. Returns whether it updated.
        """
        ...
