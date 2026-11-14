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
    #: 재개 턴이면 이 세션이 앞서 붙인 MCP 서버 (SessionRecord.mcp_servers).
    mcp_servers: str = ""
    #: 새 세션을 연 까닭. "threshold" 는 컨텍스트 임계 리셋이다. "" 는 그냥
    #: 기록이 없거나 만료된 경우다.
    reset_reason: str = ""
    #: 임계 리셋일 때 직전 세션의 호출당 컨텍스트 토큰.
    previous_context_tokens: int = 0

#: SessionDecision.reset_reason 값. 감사 기록 context_reset_reason 에 그대로 간다.
THRESHOLD_RESET_REASON = "threshold"
#: 세션을 연 턴과 이번 턴의 시스템 지침 지문이 다르다. 지침 파일을 고친 뒤다.
SYSTEM_PROMPT_RESET_REASON = "system_prompt"


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

    def resolve(
        self, key: SessionKey, engine: str, tier: str = "", fingerprint: str = "",
    ) -> SessionDecision:
        """Decide whether this conversation has a session to resume.

        No record, expired TTL, an engine mismatch (passing another
        engine's session ID through would break resumption), or a speaker
        tier other than the one that opened the session all start a new
        session with a full rebuild. Otherwise resume: TTL is renewed and
        last-seen becomes `after_ts`.

        The tier check exists because a resumed session keeps the opening
        turn's instructions. claude 2.1.319 drops --append-system-prompt on
        --resume: the delegated turn of session 4efce5d9 (2026-11-07) sent
        the owner turn's system text unchanged, OWNER_NOTE included. A turn
        of another tier would run under those. The full rebuild reads the
        thread back from Slack, so the conversation still carries. An empty
        `tier` skips the check, for callers that have no speaker.

        `fingerprint` is the same check for the instruction text itself: the
        system prompt with its per-turn values left out. A prompt file edited
        after the session opened gives a different fingerprint, and resuming
        would keep the old text (2026-11-10: 11:55 prompt_chat.md edit, 12:17
        answer still under the old one). An empty `fingerprint` skips the
        check: the caller has none, or its engine re-sends this turn's
        instructions on a resumed turn anyway (codex, gemini). A row from
        before the column holds "", which no fingerprint matches, so it opens
        a new session once: which instructions it opened with is unknown,
        and a session opened under the old text is cleared by the deploy.
        """
        now = self._now()
        record = self._store.get(key)
        if record is not None and self._is_valid(record, key.scope, now, engine, tier):
            if fingerprint and record.prompt_fingerprint != fingerprint:
                return replace(
                    self._start_new(key, engine, now, tier, fingerprint),
                    reset_reason=SYSTEM_PROMPT_RESET_REASON,
                )
            if self._over_context_threshold(record):
                try:
                    started = self._start_new(key, engine, now, tier, fingerprint)
                except Exception:  # noqa: BLE001 -- 리셋이 안 되면 그냥 재개한다
                    started = None
                if started is not None:
                    return replace(
                        started,
                        reset_reason=THRESHOLD_RESET_REASON,
                        previous_context_tokens=record.last_context_tokens,
                    )
            self._store.put(self._renew(record, now))
            return SessionDecision(
                session_id=record.session_id,
                resume=True,
                rebuild_full=False,
                after_ts=record.last_seen_ts or None,
                mcp_servers=record.mcp_servers,
            )
        return self._start_new(key, engine, now, tier, fingerprint)

    def reset(
        self, key: SessionKey, engine: str, tier: str = "", fingerprint: str = "",
    ) -> SessionDecision:
        """Force a fresh session for a conversation whose continuity broke."""
        return self._start_new(key, engine, self._now(), tier, fingerprint)

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

    def record_turn(
        self, key: SessionKey, session_id: str, *, mcp_servers: str, context_tokens: int
    ) -> None:
        """끝난 턴이 붙인 MCP 서버와 호출당 컨텍스트 토큰을 세션에 남긴다.

        세션 ID 가 다르면(그 사이 다른 턴이 새 세션을 열었으면) 건드리지 않는다.
        get/put 만 쓰므로 저장소 계약은 그대로다.
        """
        record = self._store.get(key)
        if record is None or record.session_id != session_id:
            return
        self._store.put(replace(
            record, mcp_servers=mcp_servers, last_context_tokens=max(0, int(context_tokens)),
        ))

    def _over_context_threshold(self, record: SessionRecord) -> bool:
        limit = getattr(self._settings, "context_reset_tokens", 0) or 0
        return limit > 0 and record.last_context_tokens >= limit

    def should_retry_with_new_session(self, failure_reason: str) -> bool:
        """Some failures hit the same wall on a fresh session, so retrying
        only doubles the cost. See NO_RETRY_REASONS."""
        return failure_reason not in NO_RETRY_REASONS

    def _is_valid(
        self, record: SessionRecord, scope: str, now: float, engine: str, tier: str = ""
    ) -> bool:
        if record.engine != engine:
            return False
        # A row from before the tier column holds "", which no tier matches:
        # which instructions that session opened with is unknown.
        if tier and record.tier != tier:
            return False
        ttl = self._ttl_seconds(scope)
        return (now - record.updated_at) < ttl

    def _ttl_seconds(self, scope: str) -> float:
        if scope == SessionScope.CHANNEL:
            return self._settings.channel_session_ttl_days * 86400
        return self._settings.session_ttl_hours * 3600

    def _renew(self, record: SessionRecord, now: float) -> SessionRecord:
        return replace(record, updated_at=now)

    def _start_new(
        self, key: SessionKey, engine: str, now: float, tier: str = "", fingerprint: str = "",
    ) -> SessionDecision:
        session_id = self._new_session_id()
        self._store.put(SessionRecord(
            scope=key.scope,
            key=key.key,
            session_id=session_id,
            engine=engine,
            created_at=now,
            last_seen_ts="",
            updated_at=now,
            tier=tier,
            prompt_fingerprint=fingerprint,
        ))
        return SessionDecision(
            session_id=session_id, resume=False, rebuild_full=True, after_ts=None,
        )
