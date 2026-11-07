"""How many DM replies one person gets a day (E2, mametchi DM_GUIDE_DAILY_LIMIT)."""

from __future__ import annotations

from dataclasses import dataclass

from .probe import DailyCounter


@dataclass(frozen=True)
class LimitVerdict:
    allowed: bool
    count: int
    #: True only for the first request past the cap, so the person is told
    #: once and later ones are only recorded.
    notify: bool


class DailyLimit:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._counter = DailyCounter()

    def take(self, user: str, day: str) -> LimitVerdict:
        count = self._counter.add(user, day)
        if self._limit <= 0 or count <= self._limit:
            return LimitVerdict(allowed=True, count=count, notify=False)
        return LimitVerdict(allowed=False, count=count, notify=count == self._limit + 1)
