"""연결 감시 — 소켓이 끊긴 것을 스스로 알아채고, 필요하면 스스로 재기동한다.

원본 `SocketErrorWatch` 와 `health_watch` 의 판정부를 옮겼다. 재기동 자체는
프로세스를 실제로 죽이는 동작이라 이 클래스가 직접 하지 않는다 — 생성자로
주입한 `restart` 콜백에 사유만 넘긴다. 실제 종료(`os._exit`)와 보류 중 보고
저장, 되짚기 재실행 같은 부수 효과는 이 콜백을 쥔 호출부(Worker)의 몫이다.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from ..config.settings import RuntimeSettings

# 재연결·오류를 세는 창. 원본 SOCKET_ERROR_WINDOW_SEC.
SOCKET_ERROR_WINDOW_SEC = 180


class SocketErrorWatch(logging.Handler):
    """슬랙 소켓 라이브러리가 내는 오류를 센다.

    소켓이 끊어진 것을 봇 스스로 알아채는 근거다. 라이브러리가 알려주는
    콜백에 기대지 않고 로그를 직접 본다. 판별 대상 로그 문구가 바뀌어도
    건강 점검 자체는 계속 돈다.
    """

    def __init__(self, now: Callable[[], float] = time.time) -> None:
        super().__init__()
        self._now = now
        self._errors: deque[float] = deque(maxlen=200)
        self._reconnects: deque[float] = deque(maxlen=200)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
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
        """붙었다 끊기기를 되풀이한 횟수. 정상 운영에서는 0 에 가깝다."""
        cut = self._now() - window
        return sum(1 for t in self._reconnects if t >= cut)

    def error_timestamps(self) -> tuple[float, ...]:
        """기록된 오류 시각 전체. 상태 기록이 자기 창으로 세는 데 쓴다."""
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
    """연결 감시. `check()` 한 번 호출이 원본 `health_watch` 루프의 한 회차다.

    슬랙 API 는 닿는데 소켓만 계속 깨지면 소켓이 죽은 것으로 본다.

    판정의 주 근거는 **재연결 횟수**다. 정상 운영에서는 몇 시간을 돌아도
    재연결이 일어나지 않으므로 되풀이 자체가 이상이다. 오류 건수는 라이브러리
    문구에 따라 흔들려 보조로만 쓴다.

    실측 근거 — 정상 4시간 35분 동안 재연결 0회, 장애 22분 동안 128회.
    상한 4 는 이 실측에서 나왔다(RuntimeSettings.socket_reconnect_limit).
    이전 20건/180초 오류 상한은 실제 발생률 16.8건보다 높아 한 번도 발화하지
    않았다 — 지금 상한(socket_error_limit=8)은 그 교정값이다.
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
        self._reachable = reachable
        self._restart = restart
        self._settings = settings
        self._now = now
        self._healthy = True
        self._down_since: float | None = None

    def check(self) -> HealthEvent:
        ok = self._reachable()

        if not ok:
            if self._healthy:
                self._healthy = False
                self._down_since = self._now()
            return HealthEvent(kind=HealthEventKind.DOWN)

        if not self._healthy:
            since = self._down_since if self._down_since is not None else self._now()
            outage = self._now() - since
            self._healthy = True
            self._down_since = None
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
