"""Connection health monitoring: detects a dead socket and triggers a restart.

Restarting itself kills the process, so this only decides when to restart and
hands the reason to an injected `restart` callback; the actual exit and any
cleanup are the caller's job.
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from ..config.settings import RuntimeSettings
from .outage import OutageTracker

log = logging.getLogger(__name__)

SOCKET_ERROR_WINDOW_SEC = 180


class SocketErrorWatch(logging.Handler):
    """Counts socket errors/reconnects by watching the library's log output
    rather than relying on a callback."""

    def __init__(self, now: Callable[[], float] = time.time) -> None:
        super().__init__()
        self._now = now
        self._errors: deque[float] = deque(maxlen=200)
        self._reconnects: deque[float] = deque(maxlen=200)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 — a bad log record shouldn't stop the watcher, just skip it
            return
        now = self._now()
        if "on_error invoked" in msg or "Failed to " in msg:
            self._errors.append(now)
        elif "A new session has been established" in msg:
            self._reconnects.append(now)

    def recent_errors(self, window: float = SOCKET_ERROR_WINDOW_SEC) -> int:
        cut = self._now() - window
        return sum(1 for t in self._errors if t >= cut)

    def recent_reconnects(self, window: float = SOCKET_ERROR_WINDOW_SEC) -> int:
        cut = self._now() - window
        return sum(1 for t in self._reconnects if t >= cut)

    def error_timestamps(self) -> tuple[float, ...]:
        return tuple(self._errors)

    def reconnect_timestamps(self) -> tuple[float, ...]:
        return tuple(self._reconnects)

    def clear(self) -> None:
        self._errors.clear()
        self._reconnects.clear()


class HealthEventKind(Enum):
    OK = "ok"
    DOWN = "down"
    RECOVERED = "recovered"
    RESTARTED = "restarted"


@dataclass(frozen=True)
class HealthEvent:
    kind: HealthEventKind
    outage_sec: float | None = None
    reason: str = ""


class HealthMonitor:
    """Detects a dead socket while the Slack API itself is reachable.

    Reconnect count is the primary signal — a healthy connection sees zero
    reconnects over hours, so any repeated reconnecting is anomalous. Error
    count is secondary since it varies with the library's log wording.

    Thresholds are calibrated from measurement: a healthy 4h35m window saw 0
    reconnects, a 22-minute outage saw 128. The previous error threshold
    (20/180s) never fired because real-world outages ran at ~16.8/window;
    socket_error_limit=8 corrects for that.
    """

    def __init__(
        self,
        *,
        watch: SocketErrorWatch,
        reachable: Callable[[], bool],
        restart: Callable[[str], None],
        settings: RuntimeSettings,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._watch = watch
        self._restart = restart
        self._settings = settings
        self._now = now
        # Reachability transitions are also used by catch-up; kept in one place.
        self._outage = OutageTracker(reachable=reachable, now=now)

    def check(self) -> HealthEvent:
        outage = self._outage.check()

        if not self._outage.healthy:
            return HealthEvent(kind=HealthEventKind.DOWN)

        if outage is not None:
            self._watch.clear()
            return HealthEvent(kind=HealthEventKind.RECOVERED, outage_sec=outage)

        recon = self._watch.recent_reconnects()
        errs = self._watch.recent_errors()
        if recon >= self._settings.socket_reconnect_limit:
            reason = (
                f"슬랙 API 는 정상인데 소켓이 {SOCKET_ERROR_WINDOW_SEC}초 안에 "
                f"{recon}번 다시 붙었다. 같은 창의 소켓 오류 {errs}건"
            )
            self._restart(reason)
            return HealthEvent(kind=HealthEventKind.RESTARTED, reason=reason)
        if errs >= self._settings.socket_error_limit:
            reason = (
                f"슬랙 API 는 정상인데 소켓 오류가 {SOCKET_ERROR_WINDOW_SEC}초 안에 "
                f"{errs}건 발생"
            )
            self._restart(reason)
            return HealthEvent(kind=HealthEventKind.RESTARTED, reason=reason)

        return HealthEvent(kind=HealthEventKind.OK)


class SelfRestarter:
    """Exits the process so a supervisor restarts it.

    Default callback for HealthMonitor. Exits with code 1 — code 0 would look
    like a normal exit to the supervisor, which wouldn't restart it.
    """

    def __init__(
        self,
        *,
        # Return value is ignored here, but callers may care whether the
        # notification succeeded, hence object rather than None.
        notify: Callable[[str], object] | None = None,
        inflight_count: Callable[[], int] | None = None,
        grace_sec: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        exit_process: Callable[[int], None] | None = None,
        on_shutdown_start: Callable[[], None] | None = None,
    ) -> None:
        self._notify = notify
        self._inflight_count = inflight_count
        self._grace_sec = grace_sec
        self._sleep = sleep
        # os._exit rather than sys.exit: sys.exit raises, so calling it from
        # a watcher thread would only end that thread, leaving the process
        # alive with a dead socket.
        self._exit = exit_process if exit_process is not None else _hard_exit
        self._on_shutdown_start = on_shutdown_start

    def __call__(self, reason: str) -> None:
        log.error("스스로 재기동한다 : %s", reason)
        self._announce(reason)
        if self._on_shutdown_start is not None:
            try:
                self._on_shutdown_start()
            except Exception as exc:  # noqa: BLE001 — a shutdown-flag failure shouldn't block the restart itself
                log.warning("종료 표시 실패 : %s", exc)
        self._drain()
        self._exit(1)

    def _announce(self, reason: str) -> None:
        # Best-effort: if the notify channel is unreachable, restarting
        # anyway beats leaving a dead-socket process running.
        if self._notify is None:
            return
        try:
            self._notify(f"자동 재기동\n\n- 사유 : {reason}\n\n슬랙 API 는 닿는데 소켓만 끊겨 다시 띄웁니다.")
        except Exception as exc:  # noqa: BLE001 — a failed restart-reason notification shouldn't block shutdown
            log.warning("재기동 사유 알림 실패 : %s", exc)

    def _drain(self) -> None:
        # Waits for in-flight requests to finish before exiting, up to
        # grace_sec — exiting immediately loses those requests silently,
        # but waiting forever keeps a dead-socket process alive.
        if self._inflight_count is None:
            return
        waited = 0.0
        step = 0.5
        while waited < self._grace_sec:
            try:
                remaining = self._inflight_count()
            except Exception:  # noqa: BLE001 — if we can't read the in-flight count, exit rather than wait forever
                return
            if remaining <= 0:
                return
            self._sleep(step)
            waited += step


def _hard_exit(code: int) -> None:
    os._exit(code)
