"""Detects crashed workers: computes the staleness deadline and hands it to
the queue, which owns the atomic state transition.
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
