"""Decides session scope/TTL and whether context needs rebuilding.

This module only decides *whether* and *from when* to reread; actually
rereading Slack history is TranscriptBuilder's job. Slack itself is the
source of truth, not the session file — a session only ever saw the turns
the bot actually processed, never messages people exchanged among
themselves, so even a resumed session needs anything since `after_ts`
read back in.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace

from ..config.settings import RuntimeSettings
from .ports import SessionKey, SessionRecord, SessionScope, SessionStore

# A fresh session hits the same subscription limit, so this reason is
# excluded from retry. Must match the failure string the engine layer returns.
USAGE_LIMIT_REASON = "usage_limit"
#: The engine refused before running because it can't hold the requested
#: guarantee. Shared with EngineRunner, which writes it.
CAPABILITY_UNMET_REASON = "capability_unmet"
#: 엔진 로그인이 풀린 상태다. 새 세션도 같은 자격으로 붙으므로 다시 시도해도
#: 똑같이 실패한다. EngineSwitcher 가 전환 계기 이름으로도 이 값을 쓴다.
AUTH_FAILURE_REASON = "auth_failure"
#: settings 파일이 없거나 비어 명령을 못 만든 상태다. 새 세션을 열어도 같은
#: 파일이 그대로다. Must match EngineRunner.ENGINE_CONFIG_REASON (코덱스 6차
#: 리뷰 결함4) -- kept a literal here rather than imported, same reason as
#: USAGE_LIMIT_REASON above.
ENGINE_CONFIG_REASON = "engine_config"

#: Failures a new session can't get past. All four are properties of the
#: engine, the account, or its settings, not of the conversation.
NO_RETRY_REASONS = frozenset({
    USAGE_LIMIT_REASON, CAPABILITY_UNMET_REASON, AUTH_FAILURE_REASON, ENGINE_CONFIG_REASON,
})


@dataclass(frozen=True)
class SessionDecision:
    session_id: str
    resume: bool
    rebuild_full: bool
    after_ts: str | None


def _default_new_session_id() -> str:
    """Only for callers that build a manager without an engine. Real wiring
    passes the engine's own generator -- a format this side invents is a guess
    about what that engine accepts (sca-k6s)."""
    return str(uuid.uuid4())


class SessionManager:
    def __init__(
        self,
        store: SessionStore,
        settings: RuntimeSettings,
        now: Callable[[], float] = time.time,
        # The engine's own generator. See _default_new_session_id.
        new_session_id: Callable[[], str] = _default_new_session_id,
    ) -> None:
        self._store = store
        self._settings = settings
        self._now = now
        self._new_session_id = new_session_id

    def resolve(self, key: SessionKey, engine: str) -> SessionDecision:
        """Decide whether this conversation has a session to resume.

        No record, expired TTL, or an engine mismatch (passing another
        engine's session ID through would break resumption) all start a new
        session with a full rebuild. Otherwise resume: TTL is renewed and
        last-seen becomes `after_ts`.
        """
        now = self._now()
        record = self._store.get(key)
        if record is not None and self._is_valid(record, key.scope, now, engine):
            self._store.put(self._renew(record, now))
            return SessionDecision(
                session_id=record.session_id,
                resume=True,
                rebuild_full=False,
                after_ts=record.last_seen_ts or None,
            )
        return self._start_new(key, engine, now)

    def reset(self, key: SessionKey, engine: str) -> SessionDecision:
        """Force a fresh session for a conversation whose continuity broke."""
        return self._start_new(key, engine, self._now())

    def adopt_engine_session(
        self, key: SessionKey, expected_session_id: str, engine: str, actual_session_id: str
    ) -> bool:
        """Record the session ID the engine assigned itself, when it doesn't
        honor the one we generated. Without this, the next request tries to
        resume with an ID the engine doesn't recognize and loses context.
        """
        if not actual_session_id or actual_session_id == expected_session_id:
            return False
        return self._store.reassign_session_id(
            key, expected_session_id, engine, actual_session_id, self._now()
        )

    def touch(self, key: SessionKey, seen_ts: str) -> None:
        self._store.touch(key, seen_ts)

    def should_retry_with_new_session(self, failure_reason: str) -> bool:
        """Some failures hit the same wall on a fresh session, so retrying
        only doubles the cost. See NO_RETRY_REASONS."""
        return failure_reason not in NO_RETRY_REASONS

    def _is_valid(
        self, record: SessionRecord, scope: str, now: float, engine: str
    ) -> bool:
        if record.engine != engine:
            return False
        ttl = self._ttl_seconds(scope)
        return (now - record.updated_at) < ttl

    def _ttl_seconds(self, scope: str) -> float:
        if scope == SessionScope.CHANNEL:
            return self._settings.channel_session_ttl_days * 86400
        return self._settings.session_ttl_hours * 3600

    def _renew(self, record: SessionRecord, now: float) -> SessionRecord:
        return replace(record, updated_at=now)

    def _start_new(self, key: SessionKey, engine: str, now: float) -> SessionDecision:
        session_id = self._new_session_id()
        self._store.put(SessionRecord(
            scope=key.scope,
            key=key.key,
            session_id=session_id,
            engine=engine,
            created_at=now,
            last_seen_ts="",
            updated_at=now,
        ))
        return SessionDecision(
            session_id=session_id, resume=False, rebuild_full=True, after_ts=None,
        )
