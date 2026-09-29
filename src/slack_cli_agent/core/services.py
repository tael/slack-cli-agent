"""Groups the periodic runners started by a single process, so start/stop stay
paired — a runner added to `Application` but never wired into a group's start/stop
just silently never runs.

A started group can also watch itself: `PeriodicRunner` only catches Exception,
so anything else ends that one thread while the process keeps running, and a
stopped periodic task looks exactly like a quiet one (sca-2g0). Nothing is
restarted — restarting without knowing why it died repeats the same silence.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from .periodic import PeriodicRunner

log = logging.getLogger(__name__)


@runtime_checkable
class Runnable(Protocol):
    """What ServiceGroup needs from a runner. Runnable satisfies it."""

    @property
    def name(self) -> str: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def join(self, timeout: float | None = None) -> None: ...

    def is_running(self) -> bool: ...


class ServiceGroup:
    def __init__(
        self,
        runners: Iterable[Runnable],
        *,
        name: str,
        watch_interval_sec: float = 0.0,
        notify: Callable[[str], object] | None = None,
    ) -> None:
        self._runners: tuple[Runnable, ...] = tuple(runners)
        self._name = name
        self._watch_interval_sec = watch_interval_sec
        self._notify = notify
        self._watch: PeriodicRunner | None = None
        self._reported: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return self._name

    @property
    def runners(self) -> Sequence[Runnable]:
        return self._runners

    @property
    def runner_names(self) -> tuple[str, ...]:
        names = [runner.name for runner in self._runners]
        if self._watch is not None:
            names.append(self._watch.name)
        return tuple(names)

    def dead_runners(self) -> tuple[str, ...]:
        return tuple(runner.name for runner in self._runners if not runner.is_running())

    def check_alive(self) -> None:
        """Reports a runner whose thread ended. Reports the same state once."""
        dead = self.dead_runners()
        if dead == self._reported:
            return
        self._reported = dead
        if not dead:
            log.info("%s 묶음의 주기 실행기가 전부 다시 돈다", self._name)
            return
        message = f"{self._name} 묶음의 주기 실행기가 멈췄다 : {', '.join(dead)}"
        log.error("%s", message)
        if self._notify is not None:
            self._notify(message)

    def start(self) -> None:
        # If one fails partway, stop what already started before re-raising — otherwise
        # those threads keep calling external APIs for the rest of the process's life.
        started: list[Runnable] = []
        try:
            for runner in self._runners:
                runner.start()
                started.append(runner)
        except BaseException:
            for runner in started:
                runner.stop()
            raise
        if self._watch_interval_sec > 0 and self._watch is None:
            self._watch = PeriodicRunner(
                self.check_alive, self._watch_interval_sec, name=f"{self._name}_watch"
            )
        if self._watch is not None:
            self._watch.start()
        log.info("서비스 묶음 기동: %s (%s)", self._name, ", ".join(self.runner_names) or "없음")

    def stop(self) -> None:
        if self._watch is not None:
            self._watch.stop()
        for runner in self._runners:
            try:
                runner.stop()
            except Exception:
                log.exception("주기 실행기 중단 실패: %s", runner.name)

    def join(self, timeout: float | None = None) -> None:
        for runner in self._runners:
            runner.join(timeout)
        if self._watch is not None:
            self._watch.join(timeout)

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()
