"""프로세스 하나가 띄우는 주기 실행기 묶음.

주기 실행기를 어디서 띄우고 끄는지가 CLI 명령 안에 절차로 흩어져 있으면,
`Application` 에 새 실행기를 정의해도 CLI 에서 빠뜨리는 순간 그 동작은 어떤 실행
경로에서도 일어나지 않는다. 실제로 연결 점검, 끝난 작업 정리, 첨부 정리가 전부
그 형태였다.

묶음으로 만들어 기동과 종료를 함께 처리하면, 새 실행기를 그 묶음에 넣는 것만으로
양쪽이 동시에 갖춰진다. 어떤 실행기가 들어 있는지도 `runner_names` 로 밖에서
대조할 수 있어, 정의됐는데 어느 묶음에도 안 들어간 실행기를 시험으로 검출한다.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from types import TracebackType
from typing import Self

from slack_cli_agent.core.periodic import PeriodicRunner

log = logging.getLogger(__name__)


class ServiceGroup:
    """함께 기동하고 함께 종료하는 주기 실행기 묶음."""

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
        """전부 기동한다.

        도중에 실패하면 이미 기동한 것을 먼저 멈춘 뒤 예외를 올린다. 앞의 것만
        뜬 채로 남으면 그 스레드가 프로세스 종료까지 외부 API 를 계속 호출한다.
        """
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
        """전부 중단을 요청한다. 하나가 실패해도 나머지를 계속 처리한다."""
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
