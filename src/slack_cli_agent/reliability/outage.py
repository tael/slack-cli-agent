"""Tracks Slack reachability transitions.

"Is it reachable now" and "did it just recover" are different questions —
the latter needs to remember the previous check. Both the socket-restart
decision and the post-recovery catch-up trigger rely on this same transition,
so it's centralized here rather than tracked independently in each.
"""

from __future__ import annotations

import time
from collections.abc import Callable


class OutageTracker:
    """Checks reachability each round; returns outage duration on recovery."""

    def __init__(self, *, reachable: Callable[[], bool], now: Callable[[], float] = time.time) -> None:
        self._reachable = reachable
        self._now = now
        self._healthy = True
        self._down_since: float | None = None

    @property
    def healthy(self) -> bool:
        return self._healthy

    def check(self) -> float | None:
        # Returns the outage duration only on the round recovery is detected;
        # every subsequent round is None again, or a recovery-triggered
        # catch-up would run continuously.
        ok = self._reachable()

        if not ok:
            if self._healthy:
                self._healthy = False
                self._down_since = self._now()
            return None

        if self._healthy:
            return None

        since = self._down_since if self._down_since is not None else self._now()
        outage = self._now() - since
        self._healthy = True
        self._down_since = None
        return outage
