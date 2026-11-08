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
    # The speaker tier (auth/policy.turn_tier) of the turn that opened the
    # session. A resumed engine session keeps the opening turn's instructions:
    # claude 2.1.319 ignores --append-system-prompt on --resume, and codex
    # keeps the first turn's instructions in context. So a turn of another
    # tier must not resume it. "" is a row written before this column existed.
    tier: str = ""
    # 이 세션에서 지금까지 붙인 MCP 서버 (쉼표 구분, "*" 는 전부, "" 는 기록
    # 없음). 재개 턴은 이 합집합을 유지해 앞서 쓴 도구가 사라지지 않게 한다.
    mcp_servers: str = ""
    # 마지막 턴의 호출당 컨텍스트 토큰. 컨텍스트 임계 리셋이 읽는다.
    last_context_tokens: int = 0


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
