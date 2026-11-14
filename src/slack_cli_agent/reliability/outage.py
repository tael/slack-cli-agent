"""Tracks Slack reachability transitions.

"Is it reachable now" and "did it just recover" are different questions —
the latter needs to remember the previous check. Both the socket-restart
decision and the post-recovery catch-up trigger rely on this same transition,
so it's centralized here rather than tracked independently in each.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field


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


@dataclass
class NetworkOutageEscalation:
    """Decides when a *continuing* network-level (DNS/Wi-Fi) outage needs a
    fresh diagnostic snapshot -- capture-worthiness only, no capturing.

    Slack-API reachability (OutageTracker, above) and network reachability
    are different questions on purpose: the 2026-11-02 15:07-11-03 10:07
    outage left the bot process alive and the API tracker untouched --
    `Errno 49 Can't assign requested address` happened below the socket
    layer -- so nothing noticed until a reboot. Watching DNS/HTTPS
    separately is what would have caught it.

    Mirrors watchdog.py's watch_network: a continuous failure past
    capture_after_sec (180s there) earns one diagnostic capture; if the
    outage drags on, a fresh one is taken every recapture_interval_sec
    (3600s there) since conditions may shift while it's down.
    """

    capture_after_sec: float = 180.0
    recapture_interval_sec: float = 3600.0

    def needs_capture(
        self,
        *,
        elapsed_sec: float,
        already_captured: bool,
        since_last_capture_sec: float | None = None,
    ) -> bool:
        if elapsed_sec < self.capture_after_sec:
            return False
        if not already_captured:
            return True
        return since_last_capture_sec is not None and since_last_capture_sec >= self.recapture_interval_sec


@dataclass
class RealertGate:
    """Gates repeat failure alerts so a stuck outage doesn't renotify every
    poll -- only the first alert, then again after interval_sec, same
    shape as watchdog.py's REALERT_INTERVAL (3600s) for a bot that stays
    down despite an automatic revive attempt (G54).
    """

    interval_sec: float = 3600.0
    _last_alert_at: float | None = field(default=None, init=False, repr=False)

    def due(self, now: float) -> bool:
        return self._last_alert_at is None or (now - self._last_alert_at) >= self.interval_sec

    def mark_alerted(self, now: float) -> None:
        self._last_alert_at = now

    def reset(self) -> None:
        self._last_alert_at = None
