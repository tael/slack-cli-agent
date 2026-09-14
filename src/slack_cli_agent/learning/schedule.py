"""학습 배치를 돌릴 날짜를 고른다.

원본은 launchd 가 `run-learn.sh` 를 하루 한 번 불렀다. 그래서 그날 배치를
언제 돌릴지가 코드 밖에 있었고, 프로세스가 그 시각에 꺼져 있었으면 그날치는
통째로 빠졌다. 신규 코드베이스는 워커 안의 주기 실행기가 짧은 간격으로 틱을
내므로, 어느 틱에서 실제로 돌릴지를 이 판정이 정한다.

되짚는 범위를 전날 하루로 묶는다. 더 거슬러 올라가면 오래된 기록으로 지식을
다시 채우게 되고, 그 항목들이 어느 날 것인지가 꼬리표와 어긋난다.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

_DAY_FORMAT = "%Y-%m-%d"


class DailyBatchSchedule:
    """지금 돌려야 할 날짜를 돌려준다. 없으면 None."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        run_hour: int,
        is_done: Callable[[str], bool],
    ) -> None:
        if not 0 <= run_hour <= 23:
            raise ValueError(f"기준 시각은 0-23 이어야 한다 : {run_hour}")
        self._clock = clock
        self._run_hour = run_hour
        self._is_done = is_done

    def due_day(self) -> str | None:
        """오늘이 기준 시각을 넘겼고 아직 안 끝났으면 오늘, 아니면 못 돌린 전날.

        오늘을 먼저 본다. 전날이 밀려 있어도 오늘 기록이 더 최신이고, 배치가
        한 틱에 하나씩만 도는 이상 다음 틱에 전날이 다시 후보가 된다.
        """
        now = self._clock()
        today = now.strftime(_DAY_FORMAT)
        if now.hour >= self._run_hour and not self._is_done(today):
            return today
        yesterday = (now - timedelta(days=1)).strftime(_DAY_FORMAT)
        if not self._is_done(yesterday):
            return yesterday
        return None
