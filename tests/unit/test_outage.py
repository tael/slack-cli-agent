"""OutageTracker — 슬랙에 닿는지를 보고 끊겼다 돌아온 순간을 판정한다.

같은 판정을 `HealthMonitor` 와 캐치업이 각자 들고 있으면, 한쪽만 고칠 때
동작이 갈린다. 두 프로세스가 각각 이것을 쓴다 — 접수는 소켓 재기동 판정에,
워커는 복구 직후 캐치업에 쓴다.
"""

from __future__ import annotations

from slack_cli_agent.reliability.outage import OutageTracker


class Clock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


class Test복구판정:
    def test_계속_닿으면_복구가_아니다(self) -> None:
        tracker = OutageTracker(reachable=lambda: True, now=Clock())
        assert tracker.check() is None
        assert tracker.check() is None

    def test_닿지_않는_동안은_복구가_아니다(self) -> None:
        tracker = OutageTracker(reachable=lambda: False, now=Clock())
        assert tracker.check() is None
        assert tracker.check() is None

    def test_돌아오면_끊긴_시간을_돌려준다(self) -> None:
        닿는다 = [True]
        시계 = Clock()
        tracker = OutageTracker(reachable=lambda: 닿는다[0], now=시계)
        tracker.check()

        닿는다[0] = False
        tracker.check()
        시계.value += 90.0

        닿는다[0] = True
        assert tracker.check() == 90.0

    def test_복구_직후_회차는_다시_복구가_아니다(self) -> None:
        """매 회차 복구로 보면 캐치업이 쉬지 않고 돈다."""
        닿는다 = [False]
        시계 = Clock()
        tracker = OutageTracker(reachable=lambda: 닿는다[0], now=시계)
        tracker.check()
        시계.value += 10.0
        닿는다[0] = True
        assert tracker.check() == 10.0
        assert tracker.check() is None

    def test_지금_닿는지를_따로_알려준다(self) -> None:
        """복구가 아니어도 지금 닿는지는 호출부가 쓴다."""
        tracker = OutageTracker(reachable=lambda: False, now=Clock())
        tracker.check()
        assert tracker.healthy is False


class Test끊긴시간계산:
    def test_첫_회차부터_닿지_않으면_그_시점을_시작으로_본다(self) -> None:
        시계 = Clock()
        닿는다 = [False]
        tracker = OutageTracker(reachable=lambda: 닿는다[0], now=시계)
        tracker.check()
        시계.value += 5.0
        닿는다[0] = True
        assert tracker.check() == 5.0
