"""Picks which day the learning batch should analyze on a given tick.

Backfill is limited to one prior day — reaching further back would re-apply
old history whose content no longer matches its day tag.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta

_DAY_FORMAT = "%Y-%m-%d"


def _never_waiting(day: str, now: datetime) -> bool:
    return False


class DailyBatchSchedule:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        run_hour: int,
        is_done: Callable[[str], bool],
        unsettled_days: Callable[[], Sequence[str]] = tuple,
        is_waiting: Callable[[str, datetime], bool] = _never_waiting,
    ) -> None:
        if not 0 <= run_hour <= 23:
            raise ValueError(f"기준 시각은 0-23 이어야 한다 : {run_hour}")
        self._clock = clock
        self._run_hour = run_hour
        self._is_done = is_done
        self._unsettled_days = unsettled_days
        self._is_waiting = is_waiting

    def due_day(self) -> str | None:
        now = self._clock()
        today = now.strftime(_DAY_FORMAT)
        yesterday = (now - timedelta(days=1)).strftime(_DAY_FORMAT)
        # Today takes priority over a pending yesterday; a backlog day just
        # becomes the candidate again on the next tick. Days a batch left
        # unfinished stay eligible past the one-day window — the concern that
        # limited backfill, re-running old history whole, no longer applies
        # since only the channels that never finished are analyzed.
        candidates = [today] if now.hour >= self._run_hour else []
        candidates.append(yesterday)
        candidates.extend(self._unsettled_days())
        for day in candidates:
            # A day whose retries are all still on hold would otherwise keep
            # its turn every tick and starve the older ones (sca-b4o review).
            if not self._is_done(day) and not self._is_waiting(day, now):
                return day
        return None
