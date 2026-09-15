"""기동 직후 되짚기 시험."""

from __future__ import annotations

from slack_cli_agent.reliability.startup import StartupCatchup


class _Sweeper:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def test_첫_호출에_되짚는다() -> None:
    sweep = _Sweeper()
    StartupCatchup(sweep).tick()
    assert sweep.calls == 1


def test_두_번_돌고_멈춘다() -> None:
    """첫 회에 신선도 유예에 걸린 멘션은 그 뒤 볼 절차가 없다. 유예가 지난 뒤 한 번 더 본다."""
    sweep = _Sweeper()
    catchup = StartupCatchup(sweep)
    for _ in range(5):
        catchup.tick()
    assert sweep.calls == 2


def test_횟수를_정할_수_있다() -> None:
    sweep = _Sweeper()
    catchup = StartupCatchup(sweep, passes=3)
    for _ in range(5):
        catchup.tick()
    assert sweep.calls == 3


def test_되짚기가_실패해도_횟수를_쓴다() -> None:
    """실패를 무한 재시도로 두면 같은 조회가 끝없이 나간다. 재시도는 catchup_retry 몫이다."""
    calls = []

    def boom() -> None:
        calls.append(1)
        raise RuntimeError("조회 실패")

    catchup = StartupCatchup(boom)
    for _ in range(5):
        try:
            catchup.tick()
        except RuntimeError:
            pass
    assert len(calls) == 2


def test_다_돌면_끝났다고_알린다() -> None:
    catchup = StartupCatchup(_Sweeper())
    assert catchup.finished is False
    catchup.tick()
    catchup.tick()
    assert catchup.finished is True
