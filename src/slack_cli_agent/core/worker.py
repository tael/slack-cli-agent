"""Consumes the job queue. Request handling itself is injected via the
`RequestHandler` protocol, not this class's job.

`JobQueue.enqueue`'s uniqueness constraint on (channel, message_ts) only blocks
duplicates that are still pending — not ones currently running or already
finished-and-purged. `_enqueue_new` closes that gap by also checking in-memory
running/pending state before a catch-up sweep re-adds something.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable, Sequence

from ..admin.admission import AdminAdmission, ClaimUnavailable
from ..config.settings import RuntimeSettings
from ..core.context import RequestContext
from ..core.lifecycle import InflightCounter, InflightRegistry
from ..core.ports import HandleOutcome, RequestHandler
from ..jobs.heartbeat import WorkerHeartbeat
from ..jobs.ports import Job, JobQueue, ReclaimResult
from ..reliability.catchup import CatchupReport, RetryStatus
from ..reliability.ports import CatchupPort
from ..reliability.restart import APPLY_HOLD_MAX_SEC, APPLY_REQUESTER_GRACE_SEC, ApplyRequest
from ..reliability.watchjobs import ActiveWatchPort
from ..slack.reactions import ReactionMarker
from .slots import ConcurrencySlots, SlotToken, ThreadSlots

_Mark = Callable[[str, str], None]

log = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        *,
        queue: JobQueue,
        handler: RequestHandler,
        heartbeat: WorkerHeartbeat,
        catchup: CatchupPort,
        markers: ReactionMarker,
        settings: RuntimeSettings,
        worker_id: str = "worker",
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        inflight: InflightCounter | None = None,
        # Records each handling request's identity + start time so the state
        # snapshot can list what is in flight (B51). None keeps a standalone
        # Worker working — it just has nothing to list.
        active: InflightRegistry | None = None,
        # reclaim picks the mark from this: a watched message put back in the
        # queue would otherwise read as waiting while its watch runs (sca-o1e).
        watch_jobs: ActiveWatchPort | None = None,
        # None keeps the old behavior: everything recovered goes to the model.
        # A missing injection must not make a recovered request disappear.
        admin: AdminAdmission | None = None,
        # Shared with the ingress reviews so the two together stay under
        # max_concurrent, as bot.py's one `_slots` did (A16). None keeps the
        # cap inside this process.
        slots: ConcurrencySlots | None = None,
    ) -> None:
        self._queue = queue
        self._slots = slots
        self._handler = handler
        self._heartbeat = heartbeat
        self._catchup = catchup
        self._markers = markers
        self._watch_jobs = watch_jobs
        self._admin = admin
        self._settings = settings
        self._worker_id = worker_id
        self._now = now
        self._sleep = sleep
        # Shutdown checks this; without an injected one, a standalone Worker still
        # needs its own so shutdown knows whether to wait.
        self._inflight = inflight or InflightCounter()
        self._active = active or InflightRegistry()
        self._lock = threading.Lock()
        self._running: dict[int, Job] = {}
        # Catch-up-representative key -> the requests it absorbed, so the same
        # reaction mark gets applied to them once the representative finishes.
        self._skip_groups: dict[tuple[str, str], list[RequestContext]] = {}
        # None keeps the old behavior: no apply request is ever seen.
        self._restart_request: Callable[[], ApplyRequest | None] | None = None
        # The last request seen. The supervisor removes the marker right after
        # its kickstart, while this process may still be in its TERM grace.
        self._seen_request: ApplyRequest | None = None
        self._requester_grace_sec = APPLY_REQUESTER_GRACE_SEC
        self._hold_max_sec = APPLY_HOLD_MAX_SEC
        self._requester_ok: Callable[[RequestContext], bool] | None = None
        self._hold_since: float | None = None
        self._hold_abandoned: float | None = None
        # Wall-clock start per running job: only a turn already running when
        # the marker was written can be the one that wrote it.
        self._started_at: dict[int, float] = {}

    def watch_restart_requests(
        self,
        pending: Callable[[], ApplyRequest | None],
        *,
        requester_grace_sec: float = APPLY_REQUESTER_GRACE_SEC,
        hold_max_sec: float = APPLY_HOLD_MAX_SEC,
        requester_ok: Callable[[RequestContext], bool] | None = None,
    ) -> None:
        """Polled before every claim. While a request is pending the worker
        claims nothing new and waits for the supervisor's TERM; it does not
        exit by itself, or the supervisor's kickstart restarts it a second
        time (mametchi watchdog.py take_over_sca_apply). `requester_ok` says
        whose turn may be the requester the marker names."""
        self._restart_request = pending
        self._requester_grace_sec = requester_grace_sec
        self._hold_max_sec = hold_max_sec
        self._requester_ok = requester_ok

    def _observe_request(self) -> ApplyRequest | None:
        request = self._restart_request() if self._restart_request is not None else None
        if request is not None:
            self._seen_request = request
        return request

    def holding_claims(self) -> bool:
        """True while an apply request waits for the supervisor. Released when
        the marker goes away, or after hold_max_sec if nobody picks it up
        (no supervisor running): holding forever would stop all replies."""
        request = self._observe_request()
        with self._lock:
            if request is None:
                self._hold_since = None
                return False
            if self._hold_abandoned == request.requested_at:
                return False
            now = time.monotonic()
            if self._hold_since is None:
                self._hold_since = now
                log.warning("재기동 요청이 있어 새 작업을 집지 않는다 : %s", request.turn_id)
            if now - self._hold_since < self._hold_max_sec:
                return True
            self._hold_since = None
            self._hold_abandoned = request.requested_at
        log.warning("재기동 요청을 %.0f초 동안 아무도 집지 않아 작업을 다시 집는다", self._hold_max_sec)
        return False

    def _requester_job(self) -> Job | None:
        """The running job whose turn wrote the apply request, if any. The
        marker's turn id is only trusted for a job that was already running
        when it was written and that requester_ok accepts; anything else
        would let one turn mark another person's request finished."""
        request = self._observe_request() or self._seen_request
        key = request.requester() if request is not None else None
        if request is None or key is None:
            return None
        with self._lock:
            for job in self._running.values():
                if job.context.key != key:
                    continue
                started = self._started_at.get(job.id)
                if started is None or math.floor(started) > request.requested_at:
                    return None
                if self._requester_ok is not None and not self._requester_ok(job.context):
                    return None
                return job
        return None

    def restart_requester_discount(self, waited: float) -> int:
        """How many in-flight requests shutdown stops waiting for after
        `waited` seconds: the requesting turn, once its grace is over. Waiting
        on it longer is the deadlock the marker exists to avoid."""
        if waited < self._requester_grace_sec:
            return 0
        return 1 if self._requester_job() is not None else 0

    @property
    def worker_id(self) -> str:
        # Also used as the catch-up epoch lease owner, so it must differ per process.
        return self._worker_id

    @property
    def inflight(self) -> InflightCounter:
        return self._inflight

    @property
    def active(self) -> InflightRegistry:
        return self._active

    def run_once(self) -> bool:
        job, buried = self._queue.claim_next_representative(self._worker_id)
        if job is None:
            return False
        self._bury_behind(job, buried)
        self._process(job)
        return True

    def _bury_behind(self, job: Job, buried: list[RequestContext]) -> None:
        """Tracks the requests collapsed behind this representative so they get
        the same reaction mark once it finishes, the same path a catch-up
        representative's buried messages take (A30)."""
        if not buried:
            return
        with self._lock:
            self._skip_groups.setdefault(job.context.key, []).extend(buried)

    def _process(self, job: Job) -> None:
        context = job.context
        with self._lock:
            self._running[job.id] = job
            self._started_at[job.id] = time.time()
        # The wait is over, so the queued mark gives way to the processing one.
        # Leaving it on would put both marks on every request at once.
        self._markers.clear_waiting(context.channel, context.ts)
        self._markers.mark_processing(context.channel, context.ts)
        # The eyes mark is the first reaction a request gets; stamp it so the
        # handler can record the 접수→첫반응 latency (B38). Keep an already-set
        # stamp from an earlier run so requeue/recovery doesn't push it later.
        if context.first_reaction_at is None:
            context = context.with_first_reaction(self._now())

        stop_event = threading.Event()
        beat = threading.Thread(
            target=self._heartbeat_loop, args=(job.id, job.lease, stop_event), daemon=True
        )
        beat.start()
        try:
            # Only the actual handling counts as in-flight — claiming and
            # recording completion are quick, and it's the handling that shutdown
            # needs to wait for.
            with self._inflight.work(), self._active.track(
                context.channel, context.ts, self._now()
            ):
                outcome = self._safe_handle(context)
        finally:
            stop_event.set()
            beat.join()

        with self._lock:
            still_running = self._running.pop(job.id, None) is not None
            self._started_at.pop(job.id, None)
        if not still_running:
            # shutdown() already requeued this job; don't finish it a second time.
            return

        self._finish(job, context, outcome)

    def run_forever(self, should_stop: Callable[[], bool]) -> None:
        # Sleeps on an empty queue so this doesn't busy-poll SQLite; checks should_stop
        # before claiming so a job doesn't get claimed right as shutdown begins and
        # then sit stuck until the stale-job timeout.
        #
        # Claiming stays on this one loop; only the handling goes to a thread. The
        # queue already refuses two jobs on the same thread_ts, so different threads
        # are free to run at once -- what serialized them was this process handling
        # one at a time (sca-si6). max_concurrent is that cap, same as the original
        # bot.py's semaphore.
        size = max(1, int(self._settings.max_concurrent))
        if size == 1 and self._slots is None:
            while not should_stop():
                if self.holding_claims() or not self.run_once():
                    self._sleep(self._settings.queue_idle_sleep_sec)
            return

        free = self._slots if self._slots is not None else ThreadSlots(size)
        running: list[tuple[threading.Thread, Job]] = []
        while not should_stop():
            # One should_stop call per turn, same as the single-slot loop: the
            # caller's flag is also what the tests step through. The wait is
            # bounded so a pool held by reviews still lets shutdown through.
            if self.holding_claims():
                self._sleep(self._settings.queue_idle_sleep_sec)
                continue
            token = free.acquire(timeout=self._settings.queue_idle_sleep_sec)
            if token is None:
                continue
            # A request can arrive while every slot was busy; the slot that
            # just freed must not take new work past it.
            if self.holding_claims():
                free.release(token)
                self._sleep(self._settings.queue_idle_sleep_sec)
                continue
            job, buried = self._queue.claim_next_representative(self._worker_id)
            if job is None:
                free.release(token)
                self._sleep(self._settings.queue_idle_sleep_sec)
                continue
            self._bury_behind(job, buried)
            thread = threading.Thread(
                target=self._process_and_release, args=(job, free, token), daemon=True
            )
            thread.start()
            running.append((thread, job))
            running = [(t, j) for t, j in running if t.is_alive()]
        # A job left mid-handling would keep its processing mark and be finished
        # by nobody, so the loop does not return before its threads are done --
        # except the turn that asked for a restart, which gets its grace only
        # and is then left to shutdown() to keep out of the queue.
        requester = self._requester_job()
        for thread, job in running:
            if requester is not None and job.id == requester.id:
                thread.join(self._requester_grace_sec)
            else:
                thread.join()

    def _process_and_release(
        self, job: Job, free: ConcurrencySlots, token: SlotToken
    ) -> None:
        try:
            self._process(job)
        finally:
            free.release(token)

    def _safe_handle(self, context: RequestContext) -> HandleOutcome:
        # RequestHandler is contracted not to raise, but if it does anyway, only this
        # job should fail — not take down the whole worker thread.
        try:
            return self._handler.handle(context)
        except Exception as exc:
            log.exception("처리기 예외로 작업 실패: channel=%s ts=%s", context.channel, context.ts)
            return HandleOutcome(ok=False, failure=str(exc))

    def _heartbeat_loop(self, job_id: int, lease: str, stop_event: threading.Event) -> None:
        interval = self._settings.heartbeat_interval_sec
        while not stop_event.is_set():
            self._sleep(interval)
            if stop_event.is_set():
                break
            self._queue.heartbeat(job_id, lease)

    def _finish(self, job: Job, context: RequestContext, outcome: HandleOutcome) -> None:
        if outcome.ok:
            recorded = self._queue.complete(job.id, True, "", lease=job.lease)
            mark, buried_mark = self._pick_done_marks(outcome)
        else:
            recorded = self._queue.complete(job.id, False, outcome.failure, lease=job.lease)
            mark = buried_mark = self._markers.mark_failed
        if not recorded:
            # This attempt was already reclaimed and someone else is running the
            # request now. Marking it done would show the user a finished request
            # that is still in progress (sca-ubf).
            log.warning(
                "이 점유의 결과가 아니라 표식을 안 단다 : %s:%s", context.channel, context.ts
            )
            return

        mark(context.channel, context.ts)
        # Handling now runs on several threads while catch-up fills this from
        # the claim loop, so the read-and-clear is taken under the lock.
        with self._lock:
            buried_all = self._skip_groups.pop(context.key, [])
        for buried in buried_all:
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
            # Back to queued, so the mark goes back to queued too -- unless a
            # watch is already running on that message.
            self._markers.clear_processing(context.channel, context.ts)
            if self._watch_jobs is not None and self._watch_jobs.active_watch(
                context.channel, context.ts
            ):
                self._markers.mark_watch(context.channel, context.ts)
                continue
            self._markers.mark_waiting(context.channel, context.ts)
        for context in result.failed:
            self._markers.mark_failed(context.channel, context.ts)
        return result

    def _admit(
        self, contexts: Sequence[RequestContext]
    ) -> tuple[list[RequestContext], set[tuple[str, str]]]:
        """Splits recovered requests into the ones the model still has to
        answer and the threads that ended here.

        An admin command never reaches the queue, so nothing else marks it.
        Without a mark the next sweep finds it again and runs it a second
        time (codex review).
        """
        if self._admin is None:
            return list(contexts), set()

        remaining: list[RequestContext] = []
        ended: set[tuple[str, str]] = set()
        for context in contexts:
            try:
                handled = self._admin.handled(context)
            except ClaimUnavailable:
                # Nobody ran it and nothing was marked, so the next sweep
                # finds it again. Left unmarked on purpose (sca-8m5p).
                log.warning("관리 명령 점유를 못 해 넘긴다 : %s:%s", context.channel, context.ts)
                ended.add((context.channel, context.thread_ts))
                continue
            except Exception as exc:  # noqa: BLE001 - see below
                # The dispatch may already have run part of the command
                # before raising, so passing it to the model would repeat
                # those effects (codex review).
                log.warning("관리 명령 판정 실패 : %s:%s : %s", context.channel, context.ts, exc)
                self._markers.mark_failed(context.channel, context.ts)
                ended.add((context.channel, context.thread_ts))
                continue
            if handled:
                # The mark itself is AdminAdmission's job, so the socket path
                # leaves the same trace (sca-sk9t).
                ended.add((context.channel, context.thread_ts))
            else:
                remaining.append(context)
        return remaining, ended

    def _enqueue_new(self, contexts: Sequence[RequestContext]) -> list[RequestContext]:
        with self._lock:
            running_keys = {job.context.key for job in self._running.values()}
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
                # Same gate as the sweep below: a command found only on the
                # retry is still a command (codex review).
                self._enqueue_new(self._admit(status.missed)[0])
        return statuses

    def catch_up(self, channels: list[str], window_sec: float | None = None) -> CatchupReport:
        # Accepts an explicit window because an outage can outlast the default one,
        # which would otherwise leave the earlier part of the gap unswept.
        window = window_sec if window_sec is not None else self._settings.catchup_window_sec
        report = self._catchup.sweep(channels, window)
        # The socket path decides this before queueing; a request recovered
        # here has to meet the same decision or an admin command that arrived
        # while the bot was down goes to the model (sca-oyku).
        missed, ended = self._admit(report.missed)
        accepted = self._enqueue_new(missed)

        # A representative that got filtered out (already pending/running) still
        # needs its buried messages tracked — otherwise they never get a reaction
        # mark, and the next catch-up picks one of them as its own representative
        # and answers something already answered.
        rep_key_by_thread = {
            (context.channel, context.thread_ts): context.key for context in missed
        }
        for buried in report.skipped:
            thread = (buried.channel, buried.thread_ts)
            rep_key = rep_key_by_thread.get(thread)
            if rep_key is not None:
                with self._lock:
                    self._skip_groups.setdefault(rep_key, []).append(buried)
            elif thread in ended:
                # Its representative was an admin command, so no job will
                # ever finish and mark these (codex review).
                self._markers.mark_done(buried.channel, buried.ts)

        return CatchupReport(missed=accepted, skipped=report.skipped, unchecked_channels=report.unchecked_channels)

    def shutdown(self) -> None:
        # Looked up before _running is cleared: it reads the running set.
        requester = self._requester_job()
        with self._lock:
            running = list(self._running.values())
            self._running.clear()
            self._started_at.clear()
        for job in running:
            if requester is not None and job.id == requester.id:
                # Requeued, the same turn would run again on the new process
                # and write the marker again (restart loop). Its reply is
                # already out; it only waited on the restart (mametchi bot.py
                # drop_restart_dropped).
                if self._queue.complete(job.id, True, "", lease=job.lease):
                    self._markers.clear_processing(job.context.channel, job.context.ts)
                    self._markers.mark_done(job.context.channel, job.context.ts)
                continue
            if not self._queue.requeue(job.id, lease=job.lease):
                continue
            self._markers.clear_processing(job.context.channel, job.context.ts)
            self._markers.mark_waiting(job.context.channel, job.context.ts)
