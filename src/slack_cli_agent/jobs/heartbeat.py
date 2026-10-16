"""Detects crashed workers: computes the staleness deadline and hands it to
the queue, which owns the atomic state transition.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Collection

from ..config.settings import RuntimeSettings
from .ports import ReclaimResult, StaleJobReclaimer

log = logging.getLogger(__name__)


class WorkerHeartbeat:
    def __init__(
        self,
        queue: StaleJobReclaimer,
        settings: RuntimeSettings,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._queue = queue
        self._settings = settings
        self._now = now

    def reclaim_stale(self, exclude_ids: Collection[int] = ()) -> ReclaimResult:
        """`exclude_ids` are the jobs this process is still handling.

        Staleness is read off the wall clock, and this machine suspends: after
        a wake the heartbeat row is hours old while the job is still alive,
        because the beating thread has not come out of its sleep yet. Without
        this exclusion that live attempt was requeued and started a second
        time, and its own result was later dropped for a stale lease
        (sca-rei-spin).
        """
        deadline = self._now() - self._settings.heartbeat_stale_sec
        result = self._queue.reclaim_stale(
            deadline, self._settings.job_max_attempts, exclude_ids
        )
        if result.total:
            log.warning(
                "정체 작업 처리: 재대기 %d건, 실패 %d건",
                len(result.requeued), len(result.failed),
            )
        return result
