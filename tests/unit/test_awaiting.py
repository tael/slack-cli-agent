"""답 대기 중 상태 저장소 — session/awaiting.py.

bot.py:1513-1518, 8281-8312 의 AWAITING_FILE/set_awaiting/is_awaiting 상당.
재기동을 넘겨 "이 스레드는 답을 기다리는 중" 이라는 상태가 남아야 짧은
승인("좋아")이 몇 시간 뒤에 와도 맞장구로 버려지지 않는다.
"""

from __future__ import annotations

import pytest

from slack_cli_agent.session.awaiting import AwaitingPort, SqliteAwaitingStore


@pytest.fixture
def 시계() -> dict[str, float]:
    return {"값": 1000.0}


@pytest.fixture
def store(database, 시계) -> SqliteAwaitingStore:
    return SqliteAwaitingStore(database, now=lambda: 시계["값"])


class Test계약:
    def test_구현이_포트_계약을_만족한다(self, store: SqliteAwaitingStore) -> None:
        assert isinstance(store, AwaitingPort)


class Test기본동작:
    def test_표시한적_없으면_대기중이_아니다(self, store: SqliteAwaitingStore) -> None:
        assert store.is_awaiting("C1", "111.1") is False

    def test_대기중으로_표시하면_읽힌다(self, store: SqliteAwaitingStore) -> None:
        store.set_awaiting("C1", "111.1", True)
        assert store.is_awaiting("C1", "111.1") is True

    def test_거짓으로_표시하면_대기중이_아니다(self, store: SqliteAwaitingStore) -> None:
        store.set_awaiting("C1", "111.1", True)
        store.set_awaiting("C1", "111.1", False)
        assert store.is_awaiting("C1", "111.1") is False

    def test_표시한적_없는데_거짓으로_지워도_안_터진다(self, store: SqliteAwaitingStore) -> None:
        store.set_awaiting("C1", "999.9", False)
        assert store.is_awaiting("C1", "999.9") is False

    def test_다시_표시하면_덮어쓴다(self, store: SqliteAwaitingStore) -> None:
        """답을 낼 때마다 덮어쓴다 — 별도로 지우는 경로가 필요 없다는 것이
        bot.py 의 set_awaiting 과 같은 설계다."""
        store.set_awaiting("C1", "111.1", True)
        store.set_awaiting("C1", "111.1", True)
        assert store.is_awaiting("C1", "111.1") is True

    def test_채널이_다르면_따로_센다(self, store: SqliteAwaitingStore) -> None:
        store.set_awaiting("C1", "111.1", True)
        assert store.is_awaiting("C2", "111.1") is False

    def test_스레드가_다르면_따로_센다(self, store: SqliteAwaitingStore) -> None:
        store.set_awaiting("C1", "111.1", True)
        assert store.is_awaiting("C1", "222.2") is False


class Test만료:
    """bot.py:1517 AWAITING_MAX_AGE_SEC — 24시간 지난 표시는 무시한다."""

    def test_기본_수명은_24시간이다(self, database) -> None:
        from slack_cli_agent.session.awaiting import DEFAULT_AWAITING_MAX_AGE_SEC

        assert DEFAULT_AWAITING_MAX_AGE_SEC == 24 * 3600

    def test_수명_안에서는_대기중이다(self, store: SqliteAwaitingStore, 시계) -> None:
        store.set_awaiting("C1", "111.1", True)
        시계["값"] += 23 * 3600
        assert store.is_awaiting("C1", "111.1") is True

    def test_수명이_지나면_대기중이_아니다(self, database, 시계) -> None:
        store = SqliteAwaitingStore(database, max_age_sec=100.0, now=lambda: 시계["값"])
        store.set_awaiting("C1", "111.1", True)
        시계["값"] += 101.0
        assert store.is_awaiting("C1", "111.1") is False

    def test_다시_표시하면_수명이_갱신된다(self, database, 시계) -> None:
        store = SqliteAwaitingStore(database, max_age_sec=100.0, now=lambda: 시계["값"])
        store.set_awaiting("C1", "111.1", True)
        시계["값"] += 90.0
        store.set_awaiting("C1", "111.1", True)
        시계["값"] += 90.0
        assert store.is_awaiting("C1", "111.1") is True
