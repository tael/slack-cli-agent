"""하루 한 번 학습 배치를 분석할 날짜를 고르는 판정 시험.

원본은 launchd 가 하루 한 번 불렀다. 신규 코드베이스는 워커 안의 주기
실행기가 짧은 간격으로 틱을 내므로, 그 틱 중 어느 것에서 실제로 실행할지를
이 판정이 정한다.
"""

from __future__ import annotations

from datetime import UTC, datetime

from slack_cli_agent.core.timezones import KST
from slack_cli_agent.learning.schedule import DailyBatchSchedule


def _clock(text: str):
    moment = datetime.fromisoformat(text).replace(tzinfo=KST)
    return lambda: moment


def test_기준_시각_전에는_오늘을_돌리지_않는다():
    """그 시각까지의 기록만으로 그날을 배우면 뒤에 남은 대화가 빠진다."""
    schedule = DailyBatchSchedule(
        clock=_clock("2026-09-14 21:59"), run_hour=22, is_done=lambda day: day == "2026-09-13"
    )
    assert schedule.due_day() is None


def test_기준_시각_이후에는_오늘을_돌린다():
    schedule = DailyBatchSchedule(clock=_clock("2026-09-14 22:00"), run_hour=22, is_done=lambda day: False)
    assert schedule.due_day() == "2026-09-14"


def test_이미_끝낸_날은_다시_돌리지_않는다():
    done = {"2026-09-14", "2026-09-13"}
    schedule = DailyBatchSchedule(
        clock=_clock("2026-09-14 23:30"), run_hour=22, is_done=lambda day: day in done
    )
    assert schedule.due_day() is None


def test_기준_시각_전이라도_전날이_안_끝났으면_전날을_돌린다():
    """프로세스가 밤에 꺼져 있었으면 그날 배치가 통째로 빠진다."""
    done = {"2026-09-12"}
    schedule = DailyBatchSchedule(
        clock=_clock("2026-09-14 09:00"), run_hour=22, is_done=lambda day: day in done
    )
    assert schedule.due_day() == "2026-09-13"


def test_전날이_끝났고_오늘은_기준_시각_전이면_돌릴_날이_없다():
    done = {"2026-09-13"}
    schedule = DailyBatchSchedule(
        clock=_clock("2026-09-14 09:00"), run_hour=22, is_done=lambda day: day in done
    )
    assert schedule.due_day() is None


def test_이틀_전까지_거슬러_올라가지_않는다():
    """놓친 날을 무한정 캐치업으면 오래된 기록으로 지식을 다시 채운다."""
    schedule = DailyBatchSchedule(
        clock=_clock("2026-09-14 09:00"), run_hour=22, is_done=lambda day: day == "2026-09-13"
    )
    assert schedule.due_day() is None


def test_오늘이_먼저다():
    """오늘이 기준 시각을 넘겼고 전날도 안 끝났으면 오늘을 먼저 돌린다."""
    schedule = DailyBatchSchedule(
        clock=_clock("2026-09-14 22:10"), run_hour=22, is_done=lambda day: False
    )
    assert schedule.due_day() == "2026-09-14"


def test_기준_시각이_범위_밖이면_거부한다():
    """25시는 영원히 오지 않는다. 그 값을 받으면 조용히 안 도는 배치가 된다."""
    for hour in (-1, 24):
        try:
            DailyBatchSchedule(clock=_clock("2026-09-14 10:00"), run_hour=hour, is_done=lambda day: True)
        except ValueError:
            continue
        raise AssertionError(f"{hour} 를 받아들였다")


def test_미완료로_남은_날은_이틀이_지나도_다시_후보가_된다():
    """한도 소진으로 미완료인 날이 되돌아보기 범위 밖으로 밀리면 승인해도
    그 날은 영영 안 돈다(sca-b4o 리뷰).
    """
    schedule = DailyBatchSchedule(
        clock=_clock("2026-09-16 23:30"), run_hour=22,
        is_done=lambda day: day != "2026-09-14",
        unsettled_days=lambda: ("2026-09-14",),
    )
    assert schedule.due_day() == "2026-09-14"


def test_미완료가_있어도_오늘이_먼저다():
    """미완료로 남은 옛 날짜가 오늘을 밀어내면 그날 기록이 늦어진다."""
    schedule = DailyBatchSchedule(
        clock=_clock("2026-09-16 23:30"), run_hour=22, is_done=lambda day: False,
        unsettled_days=lambda: ("2026-09-14",),
    )
    assert schedule.due_day() == "2026-09-16"


def test_미완료가_없으면_지금까지와_같다():
    schedule = DailyBatchSchedule(
        clock=_clock("2026-09-16 23:30"), run_hour=22,
        is_done=lambda day: True, unsettled_days=lambda: (),
    )
    assert schedule.due_day() is None

class Test대기_중인_날은_순서를_넘긴다:
    """오늘이 재시도 시각 전이면 분석할 채널이 없는데도 오늘이 계속 후보로
    나와 이미 시각이 지난 옛 날짜가 순서를 못 받는다(sca-b4o 리뷰).
    """

    def 일정(self, *, waiting: set[str], unsettled: tuple[str, ...]) -> DailyBatchSchedule:
        return DailyBatchSchedule(
            clock=lambda: datetime(2026, 9, 16, 5, 0, tzinfo=UTC),
            run_hour=4,
            is_done=lambda day: False,
            unsettled_days=lambda: unsettled,
            is_waiting=lambda day, now: day in waiting,
        )

    def test_오늘이_대기_중이면_미완료_날짜를_돈다(self) -> None:
        일정 = self.일정(waiting={"2026-09-16", "2026-09-15"}, unsettled=("2026-09-10",))
        assert 일정.due_day() == "2026-09-10"

    def test_전부_대기_중이면_돌_날이_없다(self) -> None:
        일정 = self.일정(waiting={"2026-09-16", "2026-09-15", "2026-09-10"}, unsettled=("2026-09-10",))
        assert 일정.due_day() is None

    def test_대기가_아니면_오늘이_먼저다(self) -> None:
        일정 = self.일정(waiting=set(), unsettled=("2026-09-10",))
        assert 일정.due_day() == "2026-09-16"
