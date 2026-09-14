"""Picks which day the learning batch should analyze on a given tick.

Backfill is limited to one prior day — reaching further back would re-apply
old history whose content no longer matches its day tag.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

_DAY_FORMAT = "%Y-%m-%d"


class DailyBatchSchedule:
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
        # Today takes priority over a pending yesterday; a backlog day just
        # becomes the candidate again on the next tick.
        now = self._clock()
        today = now.strftime(_DAY_FORMAT)
        if now.hour >= self._run_hour and not self._is_done(today):
            return today
        yesterday = (now - timedelta(days=1)).strftime(_DAY_FORMAT)
        if not self._is_done(yesterday):
            return yesterday
        return None
