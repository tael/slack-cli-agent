"""프로세스 종료 시 진행 중 요청을 기다리는 생명주기 관리.

원본 봇의 enter_work/leave_work/inflight_count/on_terminate 를 이관한다.
계약은 이렇다 — 종료 신호(SIGTERM/SIGINT)를 받으면 진행 중 요청이 0 이
될 때까지 기다린 뒤 프로세스를 끝낸다. 기다리지 않고 즉시 끝내면 처리
중이던 요청의 응답이 슬랙에 못 나간다.

새 요청을 막는 것은 이 모듈의 책임이 아니다 — 원본에서도 그 판단은
요청을 받는 쪽(핸들러)이 `_shutting_down` 을 확인해서 한다. 이 모듈은
"진행 중 건수를 세는 것"과 "0 이 될 때까지 기다리는 것"만 맡는다.
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

# signal.signal() 의 실제 시그니처를 그대로 옮긴 별칭이다. 시험이 주입하는
# 가짜 등록 함수도, 기본값인 signal.signal 자체도 이 형과 맞아야 한다.
SignalHandler = Callable[[int, FrameType | None], Any] | int | Handlers | None
SignalRegister = Callable[[int | signal_module.Signals, SignalHandler], SignalHandler]


class InflightCounter:
    """진행 중 요청 수를 스레드 안전하게 센다.

    `work()` 컨텍스트 매니저를 함께 제공한다. 요청 처리 중 예외가 나도
    `finally` 로 leave() 가 호출되게 하려면 enter/leave 를 손으로 짝지어
    부르는 것보다 이쪽을 쓰는 게 안전하다 — 짝을 놓치면 카운트가 어긋나
    다음 종료가 영원히 대기하거나 조기 종료된다.
    """

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
    """종료 신호를 받아 진행 중 요청이 0 이 될 때까지 기다린다.

    신호 등록(signal_register)과 시간 대기(sleep_fn)를 주입받는다.
    `signal.signal` 을 기본값으로 그대로 두면 단위 시험이 실제 프로세스
    신호를 건드리게 되고, `time.sleep` 을 그대로 쓰면 대기 상한만큼
    시험이 실제로 느려진다. 둘 다 시험에서는 반드시 가짜로 주입한다.
    """

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
        """지정한 신호마다 종료 처리 핸들러를 건다."""
        for sig in signals:
            self._signal_register(sig, self._handle_signal)

    def _handle_signal(self, signum: int, frame: object | None) -> None:
        # signal.signal 이 요구하는 핸들러 시그니처(signum, frame)를
        # 맞추기 위한 얇은 통로다. 실제 처리는 trigger() 가 한다.
        self.trigger()

    def trigger(self) -> None:
        """종료 절차를 시작한다.

        이미 시작된 뒤 다시 불리면 아무것도 안 한다 — SIGTERM 과 SIGINT
        가 겹쳐 들어와도 대기를 두 번 하지 않게 하는 원본의 방어다.
        """
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
