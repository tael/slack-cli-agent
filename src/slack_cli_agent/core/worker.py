"""큐 소비 워커.

영속 작업 큐에서 작업을 하나씩 집어 `RequestHandler` 에 넘기고 상태를
전이한다. 요청 처리 자체는 이 클래스의 책임이 아니다 — `RequestHandler`
Protocol 로 주입받는다.

처리 중에는 별도 스레드로 `JobQueue.heartbeat` 를 주기로 갱신한다. 처리기가
느리거나 막혀 있어도 이 갱신이 계속돼야 다른 워커가 정체 작업으로 잘못
회수하지 않는다.

되짚기(catch-up) 로 찾은 대표건을 큐에 넣기 전에, 이미 대기 중이거나 지금 이
워커가 실행 중인 것과 같은 (channel, ts) 를 걸러낸다. `JobQueue.enqueue` 의
중복 방어는 (channel, message_ts) 유일 제약이라 대기 중인 것은 막아 주지만,
실행 중이거나 이미 끝난 것은 막지 못한다 — 끝난 작업의 행이 남아 있으면
막히고 `purge_finished` 로 지워졌으면 안 막힌다. 그 구멍을 이 대조가 메운다.
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
        # 종료 절차가 이 값을 본다. 안 주면 이 워커 전용으로 하나 만든다 —
        # 값이 없으면 종료 신호를 받았을 때 처리 중인 요청이 있는지 알 수
        # 없어 기다리지 않고 프로세스가 끝난다.
        self._inflight = inflight or InflightCounter()
        self._lock = threading.Lock()
        # 이 워커가 지금 처리 중인 작업. job_id -> RequestContext
        self._running: dict[int, RequestContext] = {}
        # 되짚기 대표건에 묻힌 건들. 대표건의 (channel, ts) -> 묻힌 맥락 목록.
        # 대표건이 끝날 때 같은 표식을 이 목록에도 적용한다.
        self._skip_groups: dict[tuple[str, str], list[RequestContext]] = {}

    @property
    def inflight(self) -> InflightCounter:
        return self._inflight

    def run_once(self) -> bool:
        """큐에서 작업 하나를 집어 처리한다. 집을 것이 없으면 False."""
        job = self._queue.claim_next(self._worker_id)
        if job is None:
            return False

        context = job.context
        with self._lock:
            self._running[job.id] = context
        self._markers.mark_processing(context.channel, context.ts)

        stop_event = threading.Event()
        beat = threading.Thread(
            target=self._heartbeat_loop, args=(job.id, stop_event), daemon=True
        )
        beat.start()
        try:
            # 진행 중으로 세는 구간은 실제 처리뿐이다. 큐에서 집는 것과
            # 완료 기록은 짧고, 종료 대기가 기다려야 할 것은 처리다.
            with self._inflight.work():
                outcome = self._safe_handle(context)
        finally:
            stop_event.set()
            beat.join()

        with self._lock:
            still_running = self._running.pop(job.id, None) is not None
        if not still_running:
            # shutdown() 이 먼저 이 작업을 대기로 되돌렸다. 완료 처리를 또 하지 않는다.
            return True

        self._finish(job.id, context, outcome)
        return True

    def run_forever(self, should_stop: Callable[[], bool]) -> None:
        """중단 요청이 올 때까지 큐를 처리한다.

        집을 것이 없으면 쉰다. 반복하는 쪽이 `run_once()` 를 그대로 되풀이하면
        빈 큐에도 SQLite 조회가 쉬지 않고 일어나 한 코어를 계속 쓴다. 그 대기를
        여기 둬, 이 워커를 돌리는 모든 경로가 같은 동작을 하게 한다.

        중단 여부를 한 회차 전에 본다. 종료 중인데 큐를 한 번 더 집으면 그
        작업이 붙잡힌 채 남아, 다음 워커가 정체 판정 시각까지 못 집는다.
        """
        while not should_stop():
            if not self.run_once():
                self._sleep(self._settings.queue_idle_sleep_sec)

    def _safe_handle(self, context: RequestContext) -> HandleOutcome:
        """처리기가 예외를 내도 워커가 죽지 않게 감싼다.

        `RequestHandler` 의 계약은 예외를 밖으로 내지 않는 것이지만, 그
        계약이 깨져도 이 작업 하나만 실패로 끝나야 한다 — 그 스레드의 다음
        작업이 영영 안 나오면 안 된다.
        """
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
            mark = self._markers.mark_silent if outcome.silent else self._markers.mark_done
        else:
            self._queue.complete(job_id, False, outcome.failure)
            mark = self._markers.mark_failed

        mark(context.channel, context.ts)
        for buried in self._skip_groups.pop(context.key, []):
            mark(buried.channel, buried.ts)

    def reclaim(self) -> ReclaimResult:
        """정체 작업을 회수하고 각각의 표식을 되돌린다."""
        result = self._heartbeat.reclaim_stale()
        for context in result.requeued:
            # 다시 대기로 돌아갔다. 처리 중 표식을 지워 다음 시도가 깨끗하게 시작하게 한다.
            self._markers.remove(context.channel, context.ts, "eyes")
        for context in result.failed:
            self._markers.mark_failed(context.channel, context.ts)
        return result

    def _enqueue_new(self, contexts: Sequence[RequestContext]) -> list[RequestContext]:
        """아직 대기·실행 중이 아닌 것만 큐에 넣고 실제로 들어간 것을 돌려준다."""
        with self._lock:
            running_keys = {context.key for context in self._running.values()}
        pending_keys = {job.context.key for job in self._queue.pending()}

        accepted: list[RequestContext] = []
        for context in contexts:
            if context.key in pending_keys or context.key in running_keys:
                continue
            # 실패로 끝난 건은 되살아난다. 상한을 함께 넘겨, 계속 실패하는
            # 요청을 되짚기가 매 회차 되살리지 않게 한다.
            if self._queue.enqueue(context, max_attempts=self._settings.job_max_attempts):
                accepted.append(context)
                pending_keys.add(context.key)
        return accepted

    def retry_catchup(self) -> list[RetryStatus]:
        """마치지 못한 되짚기를 다시 본다.

        슬랙이 채널 기록을 빈 목록으로 주는 것은 대개 잠깐이다. 한 번 실패하고
        끝내면 그 구간에 답을 기다리는 요청이 어느 경로에서도 안 잡힌다.

        오래 못 본 채널을 사람에게 알릴지는 여기서 정하지 않는다 — 알림 경로를
        아는 것은 조립이므로 상태를 그대로 돌려준다.
        """
        statuses = self._catchup.retry_pending()
        for status in statuses:
            if status.missed:
                self._enqueue_new(status.missed)
        return statuses

    def catch_up(self, channels: list[str]) -> CatchupReport:
        """놓친 요청을 찾아 큐에 넣는다. 이미 대기·실행 중인 대표건은 거른다."""
        report = self._catchup.sweep(channels, self._settings.catchup_window_sec)
        accepted = self._enqueue_new(report.missed)

        # 걸러진 대표건도 연결 대상이다. 거른 것은 "이미 처리 예정" 이라는
        # 뜻이지 "묻힌 건을 버린다" 는 뜻이 아니다. 버리면 그 건에 표식이
        # 안 달려 다음 되짚기가 그것을 대표건으로 다시 집는다 — 이미 답한
        # 것에 또 답하게 된다.
        rep_key_by_thread = {
            (context.channel, context.thread_ts): context.key for context in report.missed
        }
        for buried in report.skipped:
            rep_key = rep_key_by_thread.get((buried.channel, buried.thread_ts))
            if rep_key is not None:
                self._skip_groups.setdefault(rep_key, []).append(buried)

        return CatchupReport(missed=accepted, skipped=report.skipped, unchecked_channels=report.unchecked_channels)

    def shutdown(self) -> None:
        """종료 중이다. 실행 중이던 작업을 대기로 되돌리고 표식도 되돌린다."""
        with self._lock:
            running = list(self._running.items())
            self._running.clear()
        for job_id, context in running:
            self._queue.requeue(job_id)
            self._markers.remove(context.channel, context.ts, "eyes")
