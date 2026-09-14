"""On shutdown, wait for in-flight requests to finish before the process exits —
otherwise a request being handled can't get its reply out to Slack. Blocking new
requests is not this module's job; that's on the request handler checking
`is_shutting_down`. This module only counts in-flight work and waits for it to
reach zero.
"""

from __future__ import annotations

import signal as signal_module
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from signal import Handlers
from types import FrameType
from typing import Any

# Mirrors the real signature of signal.signal() so both the real function and test doubles match.
SignalHandler = Callable[[int, FrameType | None], Any] | int | Handlers | None
SignalRegister = Callable[[int | signal_module.Signals, SignalHandler], SignalHandler]


class InflightCounter:
    # Prefer work() over calling enter()/leave() by hand — a missed leave() on an
    # exception path leaves the count off, so a later shutdown hangs or exits early.

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0

    def enter(self) -> None:
        with self._lock:
            self._count += 1

    def leave(self) -> None:
        with self._lock:
            self._count -= 1

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    @contextmanager
    def work(self) -> Iterator[None]:
        self.enter()
        try:
            yield
        finally:
            self.leave()


class GracefulShutdown:
    # signal_register and sleep_fn are injectable so tests don't touch real process
    # signals or actually block for the grace period.
    def __init__(
        self,
        inflight: InflightCounter,
        grace_sec: float,
        *,
        signal_register: SignalRegister = signal_module.signal,
        sleep_fn: Callable[[float], None] = time.sleep,
        poll_interval_sec: float = 0.5,
        on_shutdown_start: Callable[[int], None] | None = None,
        on_shutdown_timeout: Callable[[int], None] | None = None,
        on_shutdown_complete: Callable[[], None] | None = None,
        exit_fn: Callable[[], None] | None = None,
    ) -> None:
        self._inflight = inflight
        self._grace_sec = grace_sec
        self._signal_register = signal_register
        self._sleep_fn = sleep_fn
        self._poll_interval_sec = poll_interval_sec
        self._on_shutdown_start = on_shutdown_start
        self._on_shutdown_timeout = on_shutdown_timeout
        self._on_shutdown_complete = on_shutdown_complete
        self._exit_fn = exit_fn
        self._shutting_down = threading.Event()

    @property
    def is_shutting_down(self) -> bool:
        return self._shutting_down.is_set()

    def register(
        self,
        signals: Iterable[int] = (signal_module.SIGTERM, signal_module.SIGINT),
    ) -> None:
        for sig in signals:
            self._signal_register(sig, self._handle_signal)

    def _handle_signal(self, signum: int, frame: object | None) -> None:
        self.trigger()

    def trigger(self) -> None:
        # No-op if already shutting down, so overlapping SIGTERM/SIGINT don't wait twice.
        if self._shutting_down.is_set():
            return
        self._shutting_down.set()

        if self._on_shutdown_start:
            self._on_shutdown_start(self._inflight.count)

        waited = 0.0
        while self._inflight.count > 0 and waited < self._grace_sec:
            self._sleep_fn(self._poll_interval_sec)
            waited += self._poll_interval_sec

        left = self._inflight.count
        if left:
            if self._on_shutdown_timeout:
                self._on_shutdown_timeout(left)
        else:
            if self._on_shutdown_complete:
                self._on_shutdown_complete()

        if self._exit_fn:
            self._exit_fn()
