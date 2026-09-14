"""Groups the periodic runners started by a single process, so start/stop stay
paired — a runner added to `Application` but never wired into a group's start/stop
just silently never runs.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from types import TracebackType
from typing import Self

from slack_cli_agent.core.periodic import PeriodicRunner

log = logging.getLogger(__name__)


class ServiceGroup:
    def __init__(self, runners: Iterable[PeriodicRunner], *, name: str) -> None:
        self._runners: tuple[PeriodicRunner, ...] = tuple(runners)
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def runners(self) -> Sequence[PeriodicRunner]:
        return self._runners

    @property
    def runner_names(self) -> tuple[str, ...]:
        return tuple(runner.name for runner in self._runners)

    def start(self) -> None:
        # If one fails partway, stop what already started before re-raising — otherwise
        # those threads keep calling external APIs for the rest of the process's life.
        started: list[PeriodicRunner] = []
        try:
            for runner in self._runners:
                runner.start()
                started.append(runner)
        except BaseException:
            for runner in started:
                runner.stop()
            raise
        log.info("서비스 묶음 기동: %s (%s)", self._name, ", ".join(self.runner_names) or "없음")

    def stop(self) -> None:
        for runner in self._runners:
            try:
                runner.stop()
            except Exception:
                log.exception("주기 실행기 중단 실패: %s", runner.name)

    def join(self, timeout: float | None = None) -> None:
        for runner in self._runners:
            runner.join(timeout)

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
