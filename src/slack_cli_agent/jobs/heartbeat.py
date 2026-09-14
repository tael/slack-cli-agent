"""워커 크래시 감지.

프로세스를 나누면 워커가 죽었을 때 실행 중 상태로 남은 작업을 되돌릴 수단이
없다. 정체 판정 기준을 계산해 큐에 넘기고 결과를 기록한다. 상태 전이의
원자성은 큐 구현이 보장한다.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from ..config.settings import RuntimeSettings
from .ports import JobQueue, ReclaimResult

log = logging.getLogger(__name__)


class WorkerHeartbeat:
    def __init__(
        self,
        queue: JobQueue,
        settings: RuntimeSettings,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._queue = queue
        self._settings = settings
        self._now = now

    def reclaim_stale(self) -> ReclaimResult:
        deadline = self._now() - self._settings.heartbeat_stale_sec
        result = self._queue.reclaim_stale(deadline, self._settings.job_max_attempts)
        if result.total:
            log.warning(
                "정체 작업 처리: 재대기 %d건, 실패 %d건",
                len(result.requeued), len(result.failed),
            )
        return result
