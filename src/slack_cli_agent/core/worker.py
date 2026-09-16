"""Consumes the job queue. Request handling itself is injected via the
`RequestHandler` protocol, not this class's job.

`JobQueue.enqueue`'s uniqueness constraint on (channel, message_ts) only blocks
duplicates that are still pending — not ones currently running or already
finished-and-purged. `_enqueue_new` closes that gap by also checking in-memory
running/pending state before a catch-up sweep re-adds something.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence

from ..config.settings import RuntimeSettings
from ..core.context import RequestContext
from ..core.lifecycle import InflightCounter
from ..core.ports import HandleOutcome, RequestHandler
from ..jobs.heartbeat import WorkerHeartbeat
from ..jobs.ports import JobQueue, ReclaimResult
from ..reliability.catchup import CatchupReport, CatchupService, RetryStatus

_Mark = Callable[[str, str], None]
from ..slack.reactions import ReactionMarker

log = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        *,
        queue: JobQueue,
        handler: RequestHandler,
        heartbeat: WorkerHeartbeat,
        catchup: CatchupService,
        markers: ReactionMarker,
        settings: RuntimeSettings,
        worker_id: str = "worker",
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        inflight: InflightCounter | None = None,
    ) -> None:
        self._queue = queue
        self._handler = handler
        self._heartbeat = heartbeat
        self._catchup = catchup
        self._markers = markers
        self._settings = settings
        self._worker_id = worker_id
        self._now = now
        self._sleep = sleep
        # Shutdown checks this; without an injected one, a standalone Worker still
        # needs its own so shutdown knows whether to wait.
        self._inflight = inflight or InflightCounter()
        self._lock = threading.Lock()
        self._running: dict[int, RequestContext] = {}
        # Catch-up-representative key -> the requests it absorbed, so the same
        # reaction mark gets applied to them once the representative finishes.
        self._skip_groups: dict[tuple[str, str], list[RequestContext]] = {}

    @property
    def worker_id(self) -> str:
        # Also used as the catch-up epoch lease owner, so it must differ per process.
        return self._worker_id

    @property
    def inflight(self) -> InflightCounter:
        return self._inflight

    def run_once(self) -> bool:
        job = self._queue.claim_next(self._worker_id)
        if job is None:
            return False

        context = job.context
        with self._lock:
            self._running[job.id] = context
        # The wait is over, so the queued mark gives way to the processing one.
        # Leaving it on would put both marks on every request at once.
        self._markers.clear_waiting(context.channel, context.ts)
        self._markers.mark_processing(context.channel, context.ts)

        stop_event = threading.Event()
        beat = threading.Thread(
            target=self._heartbeat_loop, args=(job.id, stop_event), daemon=True
        )
        beat.start()
        try:
            # Only the actual handling counts as in-flight — claiming and
            # recording completion are quick, and it's the handling that shutdown
            # needs to wait for.
            with self._inflight.work():
                outcome = self._safe_handle(context)
        finally:
            stop_event.set()
            beat.join()

        with self._lock:
            still_running = self._running.pop(job.id, None) is not None
        if not still_running:
            # shutdown() already requeued this job; don't finish it a second time.
            return True

        self._finish(job.id, context, outcome)
        return True

    def run_forever(self, should_stop: Callable[[], bool]) -> None:
        # Sleeps on an empty queue so this doesn't busy-poll SQLite; checks should_stop
        # before claiming so a job doesn't get claimed right as shutdown begins and
        # then sit stuck until the stale-job timeout.
        while not should_stop():
            if not self.run_once():
                self._sleep(self._settings.queue_idle_sleep_sec)

    def _safe_handle(self, context: RequestContext) -> HandleOutcome:
        # RequestHandler is contracted not to raise, but if it does anyway, only this
        # job should fail — not take down the whole worker thread.
        try:
            return self._handler.handle(context)
        except Exception as exc:
            log.exception("처리기 예외로 작업 실패: channel=%s ts=%s", context.channel, context.ts)
            return HandleOutcome(ok=False, failure=str(exc))

    def _heartbeat_loop(self, job_id: int, stop_event: threading.Event) -> None:
        interval = self._settings.heartbeat_interval_sec
        while not stop_event.is_set():
            self._sleep(interval)
            if stop_event.is_set():
                break
            self._queue.heartbeat(job_id)

    def _finish(self, job_id: int, context: RequestContext, outcome: HandleOutcome) -> None:
        if outcome.ok:
            self._queue.complete(job_id, True, "")
            mark, buried_mark = self._pick_done_marks(outcome)
        else:
            self._queue.complete(job_id, False, outcome.failure)
            mark = buried_mark = self._markers.mark_failed

        mark(context.channel, context.ts)
        for buried in self._skip_groups.pop(context.key, []):
            buried_mark(buried.channel, buried.ts)

    def _pick_done_marks(self, outcome: HandleOutcome) -> tuple[_Mark, _Mark]:
        """The mark for the request itself and the one for messages buried
        behind it.

        Silence wins over watching: a request answered with nothing has nothing
        to follow up on. Buried messages never get the watch mark — the watch
        job stores only the representative's ts and clears only that one, so a
        mark left on them stays forever and keeps catch-up picking the thread
        back up (codex review).
        """
        if outcome.silent:
            return self._markers.mark_silent, self._markers.mark_silent
        if outcome.watching:
            return self._markers.mark_watch, self._markers.mark_done
        return self._markers.mark_done, self._markers.mark_done

    def reclaim(self) -> ReclaimResult:
        result = self._heartbeat.reclaim_stale()
        for context in result.requeued:
            # Back to queued, so the mark goes back to queued too.
            self._markers.clear_processing(context.channel, context.ts)
            self._markers.mark_waiting(context.channel, context.ts)
        for context in result.failed:
            self._markers.mark_failed(context.channel, context.ts)
        return result

    def _enqueue_new(self, contexts: Sequence[RequestContext]) -> list[RequestContext]:
        with self._lock:
            running_keys = {context.key for context in self._running.values()}
        pending_keys = {job.context.key for job in self._queue.pending()}

        accepted: list[RequestContext] = []
        for context in contexts:
            if context.key in pending_keys or context.key in running_keys:
                continue
            # Pass the attempt cap so a request that keeps failing isn't revived by
            # catch-up on every sweep.
            if self._queue.enqueue(context, max_attempts=self._settings.job_max_attempts):
                accepted.append(context)
                pending_keys.add(context.key)
        return accepted

    def retry_catchup(self) -> list[RetryStatus]:
        # A Slack channel-history call returning empty is usually transient; giving
        # up after one failure would leave the affected requests unhandled by any
        # path. Whether to alert on a long-unseen channel is left to the caller,
        # which knows the notification path — this just returns status.
        statuses = self._catchup.retry_pending()
        for status in statuses:
            if status.missed:
                self._enqueue_new(status.missed)
        return statuses

    def catch_up(self, channels: list[str], window_sec: float | None = None) -> CatchupReport:
        # Accepts an explicit window because an outage can outlast the default one,
        # which would otherwise leave the earlier part of the gap unswept.
        window = window_sec if window_sec is not None else self._settings.catchup_window_sec
        report = self._catchup.sweep(channels, window)
        accepted = self._enqueue_new(report.missed)

        # A representative that got filtered out (already pending/running) still
        # needs its buried messages tracked — otherwise they never get a reaction
        # mark, and the next catch-up picks one of them as its own representative
        # and answers something already answered.
        rep_key_by_thread = {
            (context.channel, context.thread_ts): context.key for context in report.missed
        }
        for buried in report.skipped:
            rep_key = rep_key_by_thread.get((buried.channel, buried.thread_ts))
            if rep_key is not None:
                self._skip_groups.setdefault(rep_key, []).append(buried)

        return CatchupReport(missed=accepted, skipped=report.skipped, unchecked_channels=report.unchecked_channels)

    def shutdown(self) -> None:
        with self._lock:
            running = list(self._running.items())
            self._running.clear()
        for job_id, context in running:
            self._queue.requeue(job_id)
            self._markers.clear_processing(context.channel, context.ts)
            self._markers.mark_waiting(context.channel, context.ts)
