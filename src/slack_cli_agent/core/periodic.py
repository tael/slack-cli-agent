"""Periodic task runner, factored out so a single iteration and the stop
condition can each be unit-tested rather than living inside an untestable
`while True` loop.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)


class PeriodicRunner:
    def __init__(self, task: Callable[[], object], interval_sec: float, *, name: str = "periodic") -> None:
        self._task = task
        self._interval_sec = interval_sec
        self._name = name
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def thread(self) -> threading.Thread | None:
        return self._thread

    def run_until_stopped(self) -> None:
        # Check the stop flag before running, not after — otherwise a shutting-down
        # process makes one more external API call than it needs to.
        while not self._stop.is_set():
            try:
                self._task()
            except Exception:
                # Don't let a transient failure kill the loop for the rest of the process's life.
                log.exception("주기 작업 실패: %s", self._name)
            self._stop.wait(self._interval_sec)

    def start(self) -> None:
        # No-op if already running — two threads would write the same file concurrently.
        if self.is_running():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run_until_stopped, name=self._name, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
