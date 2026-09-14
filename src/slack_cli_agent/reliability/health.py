"""연결 감시 — 소켓이 끊긴 것을 스스로 알아채고, 필요하면 스스로 재기동한다.

원본 `SocketErrorWatch` 와 `health_watch` 의 판정부를 옮겼다. 재기동 자체는
프로세스를 실제로 죽이는 동작이라 이 클래스가 직접 하지 않는다 — 생성자로
주입한 `restart` 콜백에 사유만 넘긴다. 실제 종료(`os._exit`)와 보류 중 보고
저장, 되짚기 재실행 같은 부수 효과는 이 콜백을 쥔 호출부(Worker)의 몫이다.
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

# 재연결·오류를 세는 창. 원본 SOCKET_ERROR_WINDOW_SEC.
SOCKET_ERROR_WINDOW_SEC = 180


class SocketErrorWatch(logging.Handler):
    """슬랙 소켓 라이브러리가 내는 오류를 센다.

    소켓이 끊어진 것을 봇 스스로 알아채는 근거다. 라이브러리가 알려주는
    콜백에 기대지 않고 로그를 직접 본다. 판별 대상 로그 문구가 바뀌어도
    건강 점검 자체는 계속 실행된다.
    """

    def __init__(self, now: Callable[[], float] = time.time) -> None:
        super().__init__()
        self._now = now
        self._errors: deque[float] = deque(maxlen=200)
        self._reconnects: deque[float] = deque(maxlen=200)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 — 로그 레코드 포맷 실패로 감시 자체가 멎으면 안 된다 — 그 레코드만 건너뛴다
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
        self._restart = restart
        self._settings = settings
        self._now = now
        # 닿는지의 상태 전이는 되짚기 쪽도 쓴다. 판정을 한 곳에 둔다.
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
    """스스로 프로세스를 끝낸다. 감독 프로세스가 다시 띄운다.

    `HealthMonitor` 가 재기동이 필요하다고 판정했을 때 부르는 콜백의 기본
    구현이다. 원본 `bot.py` 의 `self_restart()` 에 대응한다.

    소켓이 죽은 채로 프로세스가 살아 있으면 슬랙 이벤트가 하나도 안 들어온다.
    사람이 알아채고 재시작해 줄 때까지 봇이 조용히 멎어 있는 것과 같아, 사람을
    기다리지 않고 스스로 나간다.

    종료 코드는 1 이다. 0 으로 나가면 감독 프로세스가 정상 종료로 보고 다시
    띄우지 않는다 — 그러면 재기동이 아니라 그냥 정지다.
    """

    def __init__(
        self,
        *,
        # 돌려주는 값은 보지 않는다. 발송 성공 여부를 쓰는 호출부가 있어
        # 반환형을 object 로 둔다 — 그 값을 여기서 판정하지 않는다.
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
        # 기본 종료는 `os._exit` 다. `sys.exit` 는 예외를 올리는 것이라
        # 감시 스레드 안에서 부르면 그 스레드만 끝나고 프로세스는 그대로
        # 산다 — 소켓이 죽은 채로 남는다.
        self._exit = exit_process if exit_process is not None else _hard_exit
        self._on_shutdown_start = on_shutdown_start

    def __call__(self, reason: str) -> None:
        log.error("스스로 재기동한다 : %s", reason)
        self._announce(reason)
        if self._on_shutdown_start is not None:
            try:
                self._on_shutdown_start()
            except Exception as exc:  # noqa: BLE001 — 종료 표시 실패가 재기동 자체를 막으면 안 된다 — 표시를 못 해도 나가는 것이 우선이다
                log.warning("종료 표시 실패 : %s", exc)
        self._drain()
        self._exit(1)

    def _announce(self, reason: str) -> None:
        """나가기 전에 사유를 알린다. 실패해도 종료는 그대로 진행한다.

        알릴 곳이 닿지 않는다고 소켓이 끊긴 프로세스를 그대로 두면, 알림이
        없는 것이 아니라 봇 자체가 멎은 채로 남는다.
        """
        if self._notify is None:
            return
        try:
            self._notify(f"자동 재기동\n\n- 사유 : {reason}\n\n슬랙 API 는 닿는데 소켓만 끊겨 다시 띄웁니다.")
        except Exception as exc:  # noqa: BLE001 — 재기동 사유 알림 실패가 종료 절차를 막으면 안 된다
            log.warning("재기동 사유 알림 실패 : %s", exc)

    def _drain(self) -> None:
        """처리 중인 요청이 끝나기를 기다린다. 상한을 넘으면 그대로 나간다.

        기다리지 않고 나가면 그 요청은 답 없이 사라진다. 반대로 무한정
        기다리면 소켓이 끊긴 채로 계속 살아 있게 된다.
        """
        if self._inflight_count is None:
            return
        waited = 0.0
        step = 0.5
        while waited < self._grace_sec:
            try:
                remaining = self._inflight_count()
            except Exception:  # noqa: BLE001 — 처리 중 건수 조회 실패 시 더 기다리지 않고 종료한다 — 무한정 기다리면 소켓이 끊긴 채로 계속 산다
                return
            if remaining <= 0:
                return
            self._sleep(step)
            waited += step


def _hard_exit(code: int) -> None:
    os._exit(code)
