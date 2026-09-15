"""Catch-up right after start.

The outage-triggered sweep only fires when Slack itself became unreachable
and came back. A process restart isn't that — Slack stays reachable, and a
mention sent while the socket is down produces no event and no outage, so
nothing ever looks for it. Measured 2026-09-15: a mention sent 3 seconds
before the socket connected was never answered.

The original bot.py ran catch_up twice at startup for this reason, the
second pass after the freshness grace period, because the first pass
deliberately skips a mention younger than catchup_grace_sec and no later
pass would see it.
"""

from __future__ import annotations

from collections.abc import Callable


class StartupCatchup:
    """Sweeps the first `passes` ticks, then does nothing."""

    def __init__(self, sweep: Callable[[], object], *, passes: int = 2) -> None:
        self._sweep = sweep
        self._remaining = passes

    @property
    def finished(self) -> bool:
        return self._remaining <= 0

    def tick(self) -> None:
        # The count is spent before the call, so a failing sweep doesn't
        # repeat forever — retrying a failed history read is catchup_retry's job.
        if self.finished:
            return
        self._remaining -= 1
        self._sweep()
