"""세션 저장소의 계약.

구현이 아니라 여기가 계약이다. `SessionManager` 는 이 Protocol 에만 의존한다.
SQLite 고유 동작은 구현(`store.SqliteSessionStore`)의 수단이고, 지켜야 할 것은
아래 docstring 이 정한다. 계약이 지켜지는지는 tests/unit/test_session.py 가
검증한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class SessionScope(str, Enum):
    """대화를 잇는 단위. 스레드는 24시간, 채널은 7일 TTL 을 쓴다(정책은 manager)."""

    THREAD = "thread"
    CHANNEL = "channel"


@dataclass(frozen=True)
class SessionKey:
    """세션 하나를 가리키는 식별자. `sessions` 테이블의 (scope, key) 에 대응한다."""

    scope: str
    key: str


@dataclass(frozen=True)
class SessionRecord:
    """저장된 세션 한 건. `sessions` 테이블의 한 행에 대응한다."""

    scope: str
    key: str
    session_id: str
    engine: str
    created_at: float
    last_seen_ts: str
    updated_at: float
    workdir: str = ""
    model: str = ""
    """실행 환경. 화자가 아니라 대화 단위로 정하고, 한 번 넓어진 값은 좁히지 않는다.

    기본값이 빈 문자열이라 이 값을 안 쓰는 호출부는 그대로 둔다.
    """


@runtime_checkable
class SessionStore(Protocol):
    def get(self, key: SessionKey) -> SessionRecord | None:
        """기록을 돌려준다. 없으면 None. TTL 판정은 이 계층의 몫이 아니다."""

    def put(self, record: SessionRecord) -> None:
        """기록을 저장한다. 같은 키가 있으면 덮어쓴다."""

    def touch(self, key: SessionKey, seen_ts: str) -> None:
        """이 대화 단위가 마지막으로 본 슬랙 시각만 갱신한다.

        `updated_at`(세션 활동 시각, TTL 판정에 쓰는 값)은 건드리지 않는다.
        기록이 없으면 아무 일도 하지 않는다.
        """

    def expire(self, before: float) -> int:
        """`updated_at` 이 `before` 이전인 기록을 지운다. 지운 건수를 돌려준다."""
        ...

    def reassign_session_id(
        self,
        key: SessionKey,
        expected_session_id: str,
        engine: str,
        actual_session_id: str,
        now: float,
    ) -> bool:
        """실행기가 새로 발급한 세션 ID로 갈아 끼운다.

        이 대화 단위가 지금도 `expected_session_id`·`engine` 그대로일 때만
        갱신한다. 그 사이 다른 요청이 이미 세션을 바꿔 놓았으면 조건이 안
        맞아 갱신하지 않는다 — 그 변경을 덮어쓰지 않기 위해서다. 반영됐으면
        참, 아니면 거짓을 돌려준다.
        """
        ...
