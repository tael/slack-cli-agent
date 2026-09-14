"""세션 스코프·TTL 판정과 맥락 복원 여부 결정.

맥락 복원 자체(슬랙 기록을 읽어 프롬프트를 다시 세우는 것)는 이 모듈의 범위가
아니다. 여기는 "다시 읽어야 하는가" 와 "어느 시각 이후를 읽어야 하는가" 만
판정한다. 실제로 슬랙을 읽는 것은 TranscriptBuilder(다른 모듈) 의 몫이다.

대화의 원본은 세션 파일이 아니라 슬랙이다. 세션이 없거나 만료되거나 엔진이
바뀌었으면 전체 대화를 다시 읽어야 하고(rebuild_full), 이어가는 세션이어도
마지막으로 본 시각 이후의 대화는 세션이 못 본 것이라 더 읽어 붙여야 한다
(after_ts). 세션이 본 것은 봇이 실제로 처리한 턴뿐이고, 사람끼리만 오간 말은
세션 기억에 없기 때문이다.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace

from ..config.settings import RuntimeSettings
from .ports import SessionKey, SessionRecord, SessionScope, SessionStore

# 구독 한도 소진 사유. 새 대화로 바꿔도 같은 벽에 부딪히므로 재시도 대상에서
# 뺀다. 엔진 계층(run_with_fallback 상당)이 돌려주는 실패 사유 문자열과 맞춘다.
USAGE_LIMIT_REASON = "usage_limit"


@dataclass(frozen=True)
class SessionDecision:
    """`resolve`/`reset` 이 돌려주는 판정 결과."""

    session_id: str
    resume: bool
    rebuild_full: bool
    after_ts: str | None


def _default_new_session_id() -> str:
    return str(uuid.uuid4())


class SessionManager:
    def __init__(
        self,
        store: SessionStore,
        settings: RuntimeSettings,
        now: Callable[[], float] = time.time,
        new_session_id: Callable[[], str] = _default_new_session_id,
    ) -> None:
        self._store = store
        self._settings = settings
        self._now = now
        self._new_session_id = new_session_id

    def resolve(self, key: SessionKey, engine: str) -> SessionDecision:
        """이 대화 단위에 이어갈 세션이 있는지 판정한다.

        - 기록이 없으면 새 세션을 만들어 저장하고 전체 재구성을 요구한다
        - 기록이 있어도 스코프 TTL 을 넘겼으면 만료로 보고 새 세션으로 대체한다
        - 기록의 엔진이 요청한 엔진과 다르면 이어가지 않는다. 다른 엔진이 발급한
          세션 ID 를 그대로 넘기면 이어가기가 그 자리에서 끊긴다
        - 그 외에는 이어간다. TTL 을 지금 시각으로 늦추고(활동이 있었으므로),
          마지막으로 본 시각을 after_ts 로 돌려준다
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
        """이어가기가 깨진 대화에 새 세션을 발급한다. 항상 새로 시작한다."""
        return self._start_new(key, engine, self._now())

    def adopt_engine_session(
        self, key: SessionKey, expected_session_id: str, engine: str, actual_session_id: str
    ) -> bool:
        """엔진이 스스로 발급한 세션 ID 를 이 대화의 매핑에 반영한다.

        원본 `persist_runner_session()` 과 같다. 엔진에 따라 우리가 만든 ID
        를 쓰지 않고 자기 ID 를 발급한다. 그것을 반영하지 않으면 다음 요청이
        엔진이 모르는 ID 로 이어받기를 시도해 대화 맥락이 끊긴다.

        바꿀 것이 없으면 저장소를 건드리지 않고 False 를 돌려준다.
        """
        if not actual_session_id or actual_session_id == expected_session_id:
            return False
        return self._store.reassign_session_id(
            key, expected_session_id, engine, actual_session_id, self._now()
        )

    def touch(self, key: SessionKey, seen_ts: str) -> None:
        """이 대화 단위가 마지막으로 본 슬랙 시각을 남긴다."""
        self._store.touch(key, seen_ts)

    def should_retry_with_new_session(self, failure_reason: str) -> bool:
        """세션 이어가기 실패 시 새 대화로 다시 시도할지 판정한다.

        한도 소진은 새 세션으로 바꿔도 같은 벽에 부딪히므로 재시도하지 않는다.
        """
        return failure_reason != USAGE_LIMIT_REASON

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
