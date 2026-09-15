"""Socket connection epochs, and the catch-up they trigger.

Ingress owns the socket but the worker runs catch-up, so "the socket just
reconnected" cannot reach the worker as a function call. Ingress records the
fact; the worker claims it and sweeps. The original single-process bot got
this for free — restarting the process was the same event as reconnecting.

Catch-up stays on the worker side: a history sweep takes tens of seconds, and
several workers may run, so claiming has to be decided in one shared place.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from ..config.settings import RuntimeSettings

log = logging.getLogger(__name__)


class ConnectionKind(str, Enum):
    INITIAL = "initial"
    RECONNECT = "reconnect"


@dataclass(frozen=True)
class ConnectionEpoch:
    generation: int
    kind: ConnectionKind
    connected_at: float
    gap_started_at: float

    @property
    def gap_sec(self) -> float:
        return max(0.0, self.connected_at - self.gap_started_at)


@dataclass(frozen=True)
class ClaimedGap:
    """Unprocessed epochs merged into the one span that has to be swept.

    Sweeping them one by one would re-read the same history for every blip in
    a burst of reconnects.
    """

    generations: tuple[int, ...]
    gap_started_at: float
    connected_at: float

    @property
    def gap_sec(self) -> float:
        return max(0.0, self.connected_at - self.gap_started_at)


class ConnectionEpochRecorder(ABC):
    """Write side, used by ingress."""

    @abstractmethod
    def record_connection(self, kind: ConnectionKind) -> ConnectionEpoch: ...

    @abstractmethod
    def note_alive(self) -> None:
        """Records that the socket was observed up.

        The next epoch's gap start comes from this. A SIGKILLed ingress can't
        record a disconnect, so the last liveness mark is the conservative
        estimate of when the gap began.
        """


class CatchupTriggerStore(ABC):
    """Read side, used by workers."""

    @abstractmethod
    def claim(self, owner: str, lease_sec: float) -> ClaimedGap | None: ...

    @abstractmethod
    def complete(self, generations: tuple[int, ...]) -> None: ...

    @abstractmethod
    def release(self, generations: tuple[int, ...]) -> None:
        """Hands the claim back so the span is picked up without waiting for
        the lease to expire."""


class ConnectionCatchupCoordinator:
    """Turns a socket reconnect into a catch-up sweep.

    Distinct from OutageTracker on purpose: that one is Web API reachability
    as seen from the worker, this one is the socket ingress holds. The API
    stayed reachable through a dead socket in the 2026-09-16 measurement, so
    one verdict can't serve both.
    """

    def __init__(
        self,
        *,
        store: CatchupTriggerStore,
        catch_up: Callable[[float], object],
        settings: RuntimeSettings,
        owner: str,
    ) -> None:
        self._store = store
        self._catch_up = catch_up
        self._settings = settings
        self._owner = owner

    def tick(self) -> None:
        claimed = self._store.claim(self._owner, lease_sec=self._settings.catchup_lease_sec)
        if claimed is None:
            return

        window = self.window_for(claimed)
        log.info(
            "소켓 재연결 캐치업: 세대 %s, 공백 %.0f초, 창 %.0f초",
            ",".join(str(g) for g in claimed.generations),
            claimed.gap_sec,
            window,
        )
        try:
            self._catch_up(window)
        except Exception:
            # Without this, nobody can pick the span up until the lease expires.
            self._store.release(claimed.generations)
            raise
        self._store.complete(claimed.generations)

    def window_for(self, claimed: ClaimedGap) -> float:
        # The grace covers the poll interval between losing the socket and
        # the last liveness mark.
        needed = claimed.gap_sec + self._settings.catchup_grace_sec
        window = max(self._settings.catchup_window_sec, needed)
        if window > self._settings.catchup_max_window_sec:
            log.warning(
                "공백 %.0f초가 조회 상한 %.0f초보다 길다. 그 이전 구간은 회수되지 않는다.",
                claimed.gap_sec,
                self._settings.catchup_max_window_sec,
            )
            return self._settings.catchup_max_window_sec
        return window
