"""하루 한 번 학습 배치를 분석할 날짜를 고르는 판정 시험.

원본은 launchd 가 하루 한 번 불렀다. 신규 코드베이스는 워커 안의 주기
실행기가 짧은 간격으로 틱을 내므로, 그 틱 중 어느 것에서 실제로 실행할지를
이 판정이 정한다.
"""

from __future__ import annotations

from datetime import datetime

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
