"""주기 실행기.

원본은 `people_loop()` 처럼 함수 안에 `while` 무한 루프를 직접 뒀다. 그 형태는
호출하면 돌아오지 않으므로 단위 시험이 그 함수를 부를 수 없고, 결과적으로 중단
조건과 예외 처리가 한 번도 검증되지 않은 채 남는다. 반복 구조를 여기로 분리해
한 회차와 중단 조건을 각각 시험 대상으로 만든다.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)


class PeriodicRunner:
    """같은 작업을 일정 간격으로 되풀이한다.

    `run_until_stopped()` 는 중단 요청이 올 때까지 돌아오지 않는다. 호출하는
    쪽이 스레드를 직접 다루지 않아도 되게 `start()` 로 별도 스레드에서 돌리는
    경로도 함께 둔다.
    """

    def __init__(self, task: Callable[[], object], interval_sec: float, *, name: str = "periodic") -> None:
        self._task = task
        self._interval_sec = interval_sec
        self._name = name
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def thread(self) -> threading.Thread | None:
        return self._thread

    def run_until_stopped(self) -> None:
        """중단 요청이 올 때까지 작업을 되풀이한다.

        중단 여부를 작업 실행 전에 본다. 한 회차를 먼저 돌고 나서 확인하면
        종료 중인 프로세스가 외부 API 를 한 번 더 호출한다.

        작업에서 난 예외를 밖으로 내지 않는다. 여기서 예외가 올라가면 반복이
        끝나고, 그 프로세스가 살아 있는 동안 이 작업은 두 번 다시 실행되지
        않는다. 외부 조회는 일시적으로 실패하므로 한 번의 실패로 반복을
        끝내면 안 된다.
        """
        while not self._stop.is_set():
            try:
                self._task()
            except Exception:
                log.exception("주기 작업 실패: %s", self._name)
            self._stop.wait(self._interval_sec)

    def start(self) -> None:
        """별도 스레드에서 반복을 시작한다. 이미 돌고 있으면 아무것도 하지 않는다.

        중복 기동을 막는다. 스레드가 둘이면 같은 파일을 동시에 쓴다.
        """
        if self.is_running():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run_until_stopped, name=self._name, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """중단을 요청한다. 대기 중이면 즉시 깨어난다."""
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
