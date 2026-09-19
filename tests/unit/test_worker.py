"""큐 소비 워커.

Worker 는 JobQueue 와 RequestHandler 를 잇는다. 처리기 자체는 대역으로
세우고, 이 시험은 워커가 큐 상태 전이·리액션 표식·캐치업 중복 제거를
정확히 하는지만 본다.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from test_admin_admission import 관리맥락

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.context import RequestContext
from slack_cli_agent.core.ports import HandleOutcome
from slack_cli_agent.guard.watch import WATCH_MARK_EMOJI
from slack_cli_agent.jobs.heartbeat import WorkerHeartbeat
from slack_cli_agent.jobs.ports import ReclaimResult
from slack_cli_agent.jobs.queue import SqliteJobQueue
from slack_cli_agent.reliability.catchup import CatchupReport, RetryStatus
from slack_cli_agent.slack.reactions import SILENT_MARK_EMOJI, ReactionMarker


def ctx(ts: str, thread: str = "", channel: str = "C1") -> RequestContext:
    return RequestContext(channel=channel, user="U1", ts=ts, thread_ts=thread or ts, text="본문")


@dataclass
class FakeHandler:
    """호출된 맥락을 기록하고 미리 정한 결과를 돌려주는 대역."""

    outcome: HandleOutcome = field(default_factory=lambda: HandleOutcome(ok=True))
    received: list[RequestContext] = field(default_factory=list)

    def handle(self, ctx: RequestContext) -> HandleOutcome:
        self.received.append(ctx)
        return self.outcome


class RaisingHandler:
    """예외를 내는 대역. 워커가 이 예외를 삼키고 실패로 완료하는지 본다."""

    def handle(self, ctx: RequestContext) -> HandleOutcome:
        raise RuntimeError("처리기 내부 오류")


@dataclass
class BlockingHandler:
    """release() 가 불릴 때까지 막혀 있는 대역. 하트비트 시험에 쓴다."""

    release_event: threading.Event = field(default_factory=threading.Event)
    entered_event: threading.Event = field(default_factory=threading.Event)

    def handle(self, ctx: RequestContext) -> HandleOutcome:
        self.entered_event.set()
        self.release_event.wait(timeout=5)
        return HandleOutcome(ok=True)

    def release(self) -> None:
        self.release_event.set()


@dataclass
class FakeCatchup:
    """CatchupService 대역. sweep 호출 인자와 미리 정한 결과만 기록한다."""

    report: CatchupReport
    calls: list[tuple[list[str], float]] = field(default_factory=list)

    retry_statuses: list[RetryStatus] = field(default_factory=list)
    retry_calls: int = 0

    def sweep(self, channels: list[str], window: float) -> CatchupReport:
        self.calls.append((channels, window))
        return self.report

    def retry_pending(self) -> list[RetryStatus]:
        self.retry_calls += 1
        return self.retry_statuses


class FakeSlackClient:
    """슬랙 리액션 API 대역. 호출만 기록한다."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []

    def reactions_add(self, channel: str, timestamp: str, name: str) -> None:
        self.calls.append(("add", channel, timestamp, name))

    def reactions_remove(self, channel: str, timestamp: str, name: str) -> None:
        self.calls.append(("remove", channel, timestamp, name))


@dataclass
class FakeReclaimQueue:
    """reclaim_stale 호출만 기록하는 대역. test_jobs.py 의 FakeQueue 와 같은 역할."""

    result: ReclaimResult

    def reclaim_stale(self, deadline: float, max_attempts: int) -> ReclaimResult:
        return self.result


def make_worker(
    *,
    database,
    handler=None,
    catchup=None,
    settings: RuntimeSettings | None = None,
    sleep=lambda s: None,
    now=time.time,
    heartbeat_queue=None,
    admin=None,
):
    from slack_cli_agent.core.worker import Worker

    settings = settings or RuntimeSettings(heartbeat_interval_sec=0.01)
    queue = SqliteJobQueue(database, now=now)
    client = FakeSlackClient()
    markers = ReactionMarker(client)
    heartbeat = WorkerHeartbeat(heartbeat_queue or queue, settings, now=now)
    worker = Worker(
        queue=queue,
        handler=handler or FakeHandler(),
        heartbeat=heartbeat,
        catchup=catchup or FakeCatchup(CatchupReport(missed=[], skipped=[], unchecked_channels=[])),
        markers=markers,
        settings=settings,
        now=now,
        sleep=sleep,
        admin=admin,
    )
    return worker, queue, client


class TestRunOnceEmpty:
    def test_큐가_비었으면_False_를_돌려주고_처리기를_안_부른다(self, database) -> None:
        handler = FakeHandler()
        worker, _queue, _client = make_worker(database=database, handler=handler)

        assert worker.run_once() is False
        assert handler.received == []


class TestRunOnceSuccess:
    def test_작업을_집으면_처리기에_맥락이_그대로_넘어가고_성공완료가_불린다(self, database) -> None:
        handler = FakeHandler(outcome=HandleOutcome(ok=True))
        worker, queue, _client = make_worker(database=database, handler=handler)
        original = ctx("1.1")
        queue.enqueue(original)

        assert worker.run_once() is True

        assert handler.received == [original]
        assert queue.counts() == {"COMPLETED": 1}

    def test_성공하면_처리_표식과_완료_표식이_남는다(self, database) -> None:
        handler = FakeHandler(outcome=HandleOutcome(ok=True))
        worker, queue, client = make_worker(database=database, handler=handler)
        queue.enqueue(ctx("1.1"))

        worker.run_once()

        assert ("add", "C1", "1.1", "eyes") in client.calls
        assert ("add", "C1", "1.1", "white_check_mark") in client.calls

    def test_작업을_잡으면_대기_표식을_뗀다(self, database) -> None:
        """접수 때 달린 모래시계가 남아 있으면 처리 중인데도 대기로 보인다."""
        worker, queue, client = make_worker(database=database)
        queue.enqueue(ctx("1.1"))

        worker.run_once()

        assert ("remove", "C1", "1.1", "hourglass") in client.calls


class TestRunOnceFailure:
    def test_실패면_완료가_False와_사유로_불리고_x_표식이_남는다(self, database) -> None:
        handler = FakeHandler(outcome=HandleOutcome(ok=False, failure="엔진 오류"))
        worker, queue, client = make_worker(database=database, handler=handler)
        queue.enqueue(ctx("1.1"))

        worker.run_once()

        assert queue.counts() == {"FAILED": 1}
        assert ("add", "C1", "1.1", "x") in client.calls


class TestRunOnceException:
    def test_처리기가_예외를_내도_워커가_죽지않고_작업이_실패로_완료된다(self, database) -> None:
        worker, queue, client = make_worker(database=database, handler=RaisingHandler())
        queue.enqueue(ctx("1.1"))

        result = worker.run_once()

        assert result is True
        assert queue.counts() == {"FAILED": 1}
        assert ("add", "C1", "1.1", "x") in client.calls
        # 큐에 RUNNING 으로 남지 않아야 그 스레드의 다음 작업이 나올 수 있다
        queue.enqueue(ctx("1.2", "1.1"))
        assert worker.run_once() is True


class TestRunOnceSilent:
    def test_침묵_결과는_입다문_표식이_달리고_완료로_빠진다(self, database) -> None:
        handler = FakeHandler(outcome=HandleOutcome(ok=True, silent=True))
        worker, queue, client = make_worker(database=database, handler=handler)
        queue.enqueue(ctx("1.1"))

        worker.run_once()

        assert queue.counts() == {"COMPLETED": 1}
        assert ("add", "C1", "1.1", "zipper_mouth_face") in client.calls
        assert ("add", "C1", "1.1", "white_check_mark") not in client.calls


class TestRunOnceWatch:
    """감시 큐에 넘긴 요청은 완료가 아니다. 워커가 완료 표식을 달면 채널에서
    끝난 것으로 보이고, catchup.already_handled 가 DONE_EMOJI 로 판정하므로
    복구 스캔에서도 빠진다. 파이프라인이 감시 표식을 달아도 워커가 그 뒤에
    덮어썼다 — 실측 2026-09-17, 슬랙 1789576179.299509 (sca-5sb).
    """

    def test_감시로_넘긴_요청은_감시_표식이_남는다(self, database) -> None:
        handler = FakeHandler(outcome=HandleOutcome(ok=True, watching=True))
        worker, queue, client = make_worker(database=database, handler=handler)
        queue.enqueue(ctx("1.1"))

        worker.run_once()

        assert ("add", "C1", "1.1", WATCH_MARK_EMOJI) in client.calls
        assert ("add", "C1", "1.1", "white_check_mark") not in client.calls

    def test_큐에서는_완료로_빠진다(self, database) -> None:
        """표식만 미완료다. 큐에 남기면 워커가 같은 요청을 다시 처리한다."""
        handler = FakeHandler(outcome=HandleOutcome(ok=True, watching=True))
        worker, queue, _client = make_worker(database=database, handler=handler)
        queue.enqueue(ctx("1.1"))

        worker.run_once()

        assert queue.counts() == {"COMPLETED": 1}

    def test_침묵이_감시보다_우선한다(self, database) -> None:
        """답을 안 하기로 한 요청은 지켜볼 것도 없다."""
        handler = FakeHandler(outcome=HandleOutcome(ok=True, silent=True, watching=True))
        worker, queue, client = make_worker(database=database, handler=handler)
        queue.enqueue(ctx("1.1"))

        worker.run_once()

        assert ("add", "C1", "1.1", SILENT_MARK_EMOJI) in client.calls
        assert ("add", "C1", "1.1", WATCH_MARK_EMOJI) not in client.calls

    def test_묻힌_건에는_감시_표식을_안_단다(self, database) -> None:
        """감시 작업은 대표 메시지의 ts 하나만 저장하고 완료 때도 그것만
        정리한다. 묻힌 건에 mag 를 달면 아무도 떼지 않아 영구히 남고, 캐치업이
        그것을 미완료로 보아 그 스레드를 다시 집는다 (코덱스 검토)."""
        handler = FakeHandler(outcome=HandleOutcome(ok=True, watching=True))
        worker, queue, client = make_worker(database=database, handler=handler)
        대표 = ctx("1.1")
        묻힌 = ctx("1.2", "1.1")
        worker._skip_groups[대표.key] = [묻힌]
        queue.enqueue(대표)

        worker.run_once()

        assert ("add", "C1", "1.1", WATCH_MARK_EMOJI) in client.calls
        assert ("add", "C1", "1.2", "white_check_mark") in client.calls
        assert ("add", "C1", "1.2", WATCH_MARK_EMOJI) not in client.calls

    def test_실패하면_감시_표식을_안_단다(self, database) -> None:
        handler = FakeHandler(outcome=HandleOutcome(ok=False, failure="터짐", watching=True))
        worker, queue, client = make_worker(database=database, handler=handler)
        queue.enqueue(ctx("1.1"))

        worker.run_once()

        assert ("add", "C1", "1.1", "x") in client.calls
        assert ("add", "C1", "1.1", WATCH_MARK_EMOJI) not in client.calls


class TestHeartbeat:
    def test_처리_중_heartbeat가_갱신된다(self, database) -> None:
        blocking = BlockingHandler()
        settings = RuntimeSettings(heartbeat_interval_sec=0.01)
        worker, queue, _client = make_worker(
            database=database, handler=blocking, settings=settings, sleep=time.sleep
        )
        queue.enqueue(ctx("1.1"))

        thread = threading.Thread(target=worker.run_once)
        thread.start()
        assert blocking.entered_event.wait(timeout=2)

        # 처리 중 최초 heartbeat_ts 값을 잡아 두고, 갱신되는지 관찰한다
        row = queue._fetch_all("SELECT heartbeat_ts FROM jobs WHERE id = 1", ())[0]
        first_ts = row["heartbeat_ts"]

        deadline = time.time() + 2
        updated = False
        while time.time() < deadline:
            row = queue._fetch_all("SELECT heartbeat_ts FROM jobs WHERE id = 1", ())[0]
            if row["heartbeat_ts"] > first_ts:
                updated = True
                break
            time.sleep(0.01)

        blocking.release()
        thread.join(timeout=2)

        assert updated is True


class TestReclaim:
    def test_되돌려진_작업과_실패한_작업의_표식을_각각_되돌린다(self, database) -> None:
        from slack_cli_agent.core.worker import Worker

        requeued_ctx = ctx("1.1")
        failed_ctx = ctx("2.1")
        fake_queue = FakeReclaimQueue(
            result=ReclaimResult(requeued=[requeued_ctx], failed=[failed_ctx])
        )
        settings = RuntimeSettings()
        heartbeat = WorkerHeartbeat(fake_queue, settings)
        client = FakeSlackClient()
        markers = ReactionMarker(client)

        worker = Worker(
            queue=SqliteJobQueue(database),
            handler=FakeHandler(),
            heartbeat=heartbeat,
            catchup=FakeCatchup(CatchupReport(missed=[], skipped=[], unchecked_channels=[])),
            markers=markers,
            settings=settings,
        )

        result = worker.reclaim()

        assert result.requeued == [requeued_ctx]
        assert result.failed == [failed_ctx]
        # 큐로 되돌아갔으니 표식도 대기로 되돌린다
        assert ("remove", "C1", "1.1", "eyes") in client.calls
        assert ("add", "C1", "1.1", "hourglass") in client.calls
        assert ("add", "C1", "2.1", "x") in client.calls


    def test_감시_중인_메시지는_대기_표식으로_안_바꾼다(self, database) -> None:
        """감시가 걸린 채로 대기 표식을 붙이면 그 메시지는 감시 중이면서 대기
        중으로 보인다(sca-o1e)."""
        from slack_cli_agent.core.worker import Worker

        class 감시대역:
            def active_watch(self, channel: str, msg_ts: str) -> bool:
                return (channel, msg_ts) == ("C1", "1.1")

        requeued_ctx = ctx("1.1")
        fake_queue = FakeReclaimQueue(result=ReclaimResult(requeued=[requeued_ctx], failed=[]))
        settings = RuntimeSettings()
        client = FakeSlackClient()

        worker = Worker(
            queue=SqliteJobQueue(database),
            handler=FakeHandler(),
            heartbeat=WorkerHeartbeat(fake_queue, settings),
            catchup=FakeCatchup(CatchupReport(missed=[], skipped=[], unchecked_channels=[])),
            markers=ReactionMarker(client),
            settings=settings,
            watch_jobs=감시대역(),
        )
        worker.reclaim()

        assert ("add", "C1", "1.1", "mag") in client.calls
        assert ("add", "C1", "1.1", "hourglass") not in client.calls

    def test_감시가_없으면_그대로_대기_표식이다(self, database) -> None:
        from slack_cli_agent.core.worker import Worker

        class 감시없음:
            def active_watch(self, channel: str, msg_ts: str) -> bool:
                return False

        fake_queue = FakeReclaimQueue(result=ReclaimResult(requeued=[ctx("1.1")], failed=[]))
        settings = RuntimeSettings()
        client = FakeSlackClient()

        worker = Worker(
            queue=SqliteJobQueue(database),
            handler=FakeHandler(),
            heartbeat=WorkerHeartbeat(fake_queue, settings),
            catchup=FakeCatchup(CatchupReport(missed=[], skipped=[], unchecked_channels=[])),
            markers=ReactionMarker(client),
            settings=settings,
            watch_jobs=감시없음(),
        )
        worker.reclaim()

        assert ("add", "C1", "1.1", "hourglass") in client.calls


class TestCatchUpDedup:
    def test_큐에_이미_대기중인_대표건은_큐에_안_들어간다(self, database) -> None:
        worker, queue, _client = make_worker(database=database)
        queue.enqueue(ctx("1.1", "T1"))  # 이미 대기 중

        report = CatchupReport(missed=[ctx("1.1", "T1")], skipped=[], unchecked_channels=[])
        worker._catchup = FakeCatchup(report)

        worker.catch_up(["C1"])

        # 처음 넣은 1건 그대로다 — 이미 대기 중인 대표건은 다시 등록되지 않는다
        assert len(queue.pending()) == 1

    def test_지금_실행중인_대표건은_큐에_안_들어간다(self, database) -> None:
        release = threading.Event()
        entered = threading.Event()

        @dataclass
        class SlowHandler:
            def handle(self, ctx: RequestContext) -> HandleOutcome:
                entered.set()
                release.wait(timeout=5)
                return HandleOutcome(ok=True)

        worker, queue, _client = make_worker(database=database, handler=SlowHandler())
        queue.enqueue(ctx("1.1", "T1"))

        thread = threading.Thread(target=worker.run_once)
        thread.start()
        assert entered.wait(timeout=2)

        report = CatchupReport(missed=[ctx("1.1", "T1")], skipped=[], unchecked_channels=[])
        worker._catchup = FakeCatchup(report)

        result = worker.catch_up(["C1"])

        release.set()
        thread.join(timeout=2)

        assert result.missed == []
        # 대표건이 실행 중이었으므로 다시 등록되지 않는다 — pending 에 새 행이 없다
        assert queue.pending() == []

    def test_skipped는_큐에_안_들어간다(self, database) -> None:
        worker, queue, _client = make_worker(database=database)
        rep = ctx("1.2", "T1")
        skip = ctx("1.1", "T1")
        report = CatchupReport(missed=[rep], skipped=[skip], unchecked_channels=[])
        worker._catchup = FakeCatchup(report)

        worker.catch_up(["C1"])

        pending_tss = {j.context.ts for j in queue.pending()}
        assert pending_tss == {"1.2"}

    def test_대표건이_처리되면_묻힌_건도_같은_표식을_받는다(self, database) -> None:
        handler = FakeHandler(outcome=HandleOutcome(ok=True))
        worker, _queue, client = make_worker(database=database, handler=handler)
        rep = ctx("1.2", "T1")
        skip = ctx("1.1", "T1")
        report = CatchupReport(missed=[rep], skipped=[skip], unchecked_channels=[])
        worker._catchup = FakeCatchup(report)

        worker.catch_up(["C1"])
        worker.run_once()

        assert ("add", "C1", "1.2", "white_check_mark") in client.calls
        assert ("add", "C1", "1.1", "white_check_mark") in client.calls


class TestShutdown:
    def test_종료하면_실행중_작업을_대기로_되돌린다(self, database) -> None:
        release = threading.Event()
        entered = threading.Event()

        @dataclass
        class SlowHandler:
            def handle(self, ctx: RequestContext) -> HandleOutcome:
                entered.set()
                release.wait(timeout=5)
                return HandleOutcome(ok=True)

        worker, queue, client = make_worker(database=database, handler=SlowHandler())
        queue.enqueue(ctx("1.1"))

        thread = threading.Thread(target=worker.run_once)
        thread.start()
        assert entered.wait(timeout=2)

        worker.shutdown()

        assert queue.counts() == {"QUEUED": 1}
        assert ("remove", "C1", "1.1", "eyes") in client.calls
        assert ("add", "C1", "1.1", "hourglass") in client.calls

        release.set()
        thread.join(timeout=2)


class Test대표건이_걸러져도_묻힌_건은_따라간다:
    def test_이미_대기중인_대표건에_묻힌_건도_같은_표식을_받는다(self, database) -> None:
        """대표건을 거르는 것과 묻힌 건을 버리는 것은 다르다.

        접수 경로로 이미 큐에 들어간 요청이 캐치업에도 잡히면 대표건은
        걸러진다. 그때 묻힌 건을 그냥 버리면 표식이 안 달려, 다음 캐치업이
        그것을 다시 대표건으로 집는다 — 이미 답한 것에 또 답하게 된다.
        """
        handler = FakeHandler(outcome=HandleOutcome(ok=True))
        worker, queue, client = make_worker(database=database, handler=handler)
        rep = ctx("1.2", "T1")
        skip = ctx("1.1", "T1")
        # 접수 경로가 이미 넣은 상태를 만든다.
        queue.enqueue(rep)
        worker._catchup = FakeCatchup(
            CatchupReport(missed=[rep], skipped=[skip], unchecked_channels=[])
        )

        worker.catch_up(["C1"])
        worker.run_once()

        assert ("add", "C1", "1.2", "white_check_mark") in client.calls
        assert ("add", "C1", "1.1", "white_check_mark") in client.calls


class TestInflight진행중건수:
    """워커가 진행 중 요청 수를 세는가.

    종료 신호를 받았을 때 처리 중인 요청이 있는지 알아야 기다릴 수 있다.
    세지 않으면 종료 절차가 즉시 끝나고, 그 요청의 응답이 슬랙에 못 나간다.
    """

    def test_처리_중에는_건수가_1이다(self, database) -> None:
        seen: list[int] = []

        class 관찰핸들러(FakeHandler):
            def handle(self, context):
                seen.append(worker.inflight.count)
                return super().handle(context)

        worker, queue, _client = make_worker(database=database, handler=관찰핸들러())
        queue.enqueue(ctx("1"))
        worker.run_once()
        assert seen == [1]

    def test_처리가_끝나면_0으로_돌아온다(self, database) -> None:
        worker, queue, _client = make_worker(database=database)
        queue.enqueue(ctx("1"))
        worker.run_once()
        assert worker.inflight.count == 0

    def test_처리기가_예외를_내도_0으로_돌아온다(self, database) -> None:
        class 예외핸들러(FakeHandler):
            def handle(self, context):
                raise RuntimeError("처리 실패")

        worker, queue, _client = make_worker(database=database, handler=예외핸들러())
        queue.enqueue(ctx("1"))
        worker.run_once()
        assert worker.inflight.count == 0


class Test빈큐대기:
    """집을 것이 없을 때 곧바로 다시 조회하지 않는가.

    `run_once()` 는 큐가 비면 바로 False 로 돌아온다. 반복하는 쪽이 그 결과를
    안 보고 다시 부르면 SQLite 조회가 쉬지 않고 되풀이돼 한 코어를 계속 쓴다.
    대기를 워커 안에 둬, 반복하는 쪽마다 따로 짜지 않게 한다.
    """

    def test_집을것이_없으면_기다린다(self, database) -> None:
        settings = RuntimeSettings(heartbeat_interval_sec=0.01)
        기다린시간: list[float] = []
        worker, _queue, _client = make_worker(
            database=database, settings=settings, sleep=기다린시간.append,
        )
        멈춤 = [False, False, True]
        worker.run_forever(lambda: 멈춤.pop(0))
        # 심장박동 스레드도 같은 대역으로 쉰다. 빈 큐 대기값만 세어 구분한다.
        쉰횟수 = [t for t in 기다린시간 if t == settings.queue_idle_sleep_sec]
        assert 쉰횟수 == [settings.queue_idle_sleep_sec] * 2

    def test_집을것이_있으면_안_기다린다(self, database) -> None:
        """처리할 것이 남아 있는데 쉬면 응답이 그만큼 늦어진다."""
        기다린시간: list[float] = []
        worker, queue, _client = make_worker(database=database, sleep=기다린시간.append)
        queue.enqueue(ctx("1.0"))
        queue.enqueue(ctx("2.0"))
        멈춤 = [False, False, True]
        worker.run_forever(lambda: 멈춤.pop(0))
        assert RuntimeSettings().queue_idle_sleep_sec not in 기다린시간

    def test_멈추라면_한_회차도_안_돈다(self, database) -> None:
        """종료 중인 프로세스가 큐를 한 번 더 집으면 그 건이 붙잡힌 채 남는다."""
        handler = FakeHandler()
        worker, queue, _client = make_worker(database=database, handler=handler)
        queue.enqueue(ctx("1.0"))
        worker.run_forever(lambda: True)
        assert handler.received == []

    def test_예외가_나도_반복이_끝나지_않는다(self, database) -> None:
        """한 건의 실패로 반복이 끝나면 그 워커는 다시 아무것도 처리하지 않는다."""
        worker, queue, _client = make_worker(database=database, handler=RaisingHandler())
        queue.enqueue(ctx("1.0"))
        queue.enqueue(ctx("2.0"))
        멈춤 = [False, False, True]
        worker.run_forever(lambda: 멈춤.pop(0))
        # 두 건 다 집혔다. 첫 건의 예외가 반복을 끝내지 않았다.
        assert queue.claim_next("other") is None


class Test캐치업이_실패건을_되살린다:
    """실패로 끝난 건을 캐치업이 다시 등록하는가.

    실패한 행이 `(channel, message_ts)` 를 계속 차지하면, 캐치업이 미응답
    멘션을 찾아내도 등록이 조용히 무시된다. 그 요청은 답을 못 받은 채로
    영영 남는다.
    """

    def test_안_먹힌_완료는_표식을_안_단다(self, database) -> None:
        """옛 워커의 늦은 결과가 지금 도는 시도의 요청에 완료 표식을 달면,
        사용자에게는 끝난 것으로 보이는데 실제로는 아직 돌고 있다."""
        worker, queue, client = make_worker(database=database)
        queue.enqueue(ctx("1.1", "T1"))
        옛시도 = queue.claim_next("죽은워커")
        assert 옛시도 is not None
        queue.reclaim_stale(deadline=time.time() + 1, max_attempts=9)
        assert queue.claim_next("산워커") is not None
        client.calls.clear()

        worker._finish(옛시도, 옛시도.context, HandleOutcome(ok=True))

        assert client.calls == []

    def test_실패한_대표건은_다시_등록된다(self, database) -> None:
        worker, queue, _client = make_worker(database=database)
        queue.enqueue(ctx("1.1", "T1"))
        job = queue.claim_next("w")
        assert job is not None
        queue.complete(job.id, ok=False, failure="엔진 오류", lease=job.lease)

        report = CatchupReport(missed=[ctx("1.1", "T1")], skipped=[], unchecked_channels=[])
        worker._catchup = FakeCatchup(report)
        accepted = worker.catch_up(["C1"])

        assert [c.ts for c in accepted.missed] == ["1.1"]
        assert [j.context.ts for j in queue.pending()] == ["1.1"]

    def test_시도상한을_넘긴건은_되살리지_않는다(self, database) -> None:
        """계속 실패하는 요청을 캐치업이 매번 되살리면 끝나지 않는다."""
        settings = RuntimeSettings(heartbeat_interval_sec=0.01, job_max_attempts=1)
        worker, queue, _client = make_worker(database=database, settings=settings)
        queue.enqueue(ctx("1.1", "T1"))
        job = queue.claim_next("w")
        assert job is not None
        queue.complete(job.id, ok=False, failure="엔진 오류", lease=job.lease)

        report = CatchupReport(missed=[ctx("1.1", "T1")], skipped=[], unchecked_channels=[])
        worker._catchup = FakeCatchup(report)
        accepted = worker.catch_up(["C1"])

        assert accepted.missed == []
        assert queue.pending() == []

    def test_완료된건은_되살리지_않는다(self, database) -> None:
        worker, queue, _client = make_worker(database=database)
        queue.enqueue(ctx("1.1", "T1"))
        job = queue.claim_next("w")
        assert job is not None
        queue.complete(job.id, ok=True, lease=job.lease)

        report = CatchupReport(missed=[ctx("1.1", "T1")], skipped=[], unchecked_channels=[])
        worker._catchup = FakeCatchup(report)
        assert worker.catch_up(["C1"]).missed == []


class Test마치지못한캐치업을다시본다:
    """슬랙이 채널 기록을 빈 목록으로 주면 그 구간의 요청이 안 잡힌다.

    한 번 실패하고 끝내면 그 요청들은 어느 경로에서도 처리되지 않는다.
    원본은 건강 점검이 돌 때마다 다시 봤다.
    """

    def test_다시_보아_찾은_요청을_큐에_넣는다(self, database) -> None:
        from slack_cli_agent.reliability.catchup import RetryStatus

        catchup = FakeCatchup(CatchupReport(missed=[], skipped=[], unchecked_channels=[]))
        catchup.retry_statuses = [RetryStatus(channel="C1", stuck_sec=0.0, alert=False, missed=(ctx("9.1", "T9"),))]
        worker, queue, _client = make_worker(database=database, catchup=catchup)

        worker.retry_catchup()

        assert [job.context.ts for job in queue.pending()] == ["9.1"]

    def test_이미_대기중인_건은_다시_안_넣는다(self, database) -> None:
        from slack_cli_agent.reliability.catchup import RetryStatus

        catchup = FakeCatchup(CatchupReport(missed=[], skipped=[], unchecked_channels=[]))
        catchup.retry_statuses = [RetryStatus(channel="C1", stuck_sec=0.0, alert=False, missed=(ctx("9.1", "T9"),))]
        worker, queue, _client = make_worker(database=database, catchup=catchup)
        queue.enqueue(ctx("9.1", "T9"))

        worker.retry_catchup()

        assert len(queue.pending()) == 1

    def test_상태를_그대로_돌려준다(self, database) -> None:
        """오래 못 본 채널을 사람에게 알릴지는 호출부가 정한다."""
        from slack_cli_agent.reliability.catchup import RetryStatus

        catchup = FakeCatchup(CatchupReport(missed=[], skipped=[], unchecked_channels=[]))
        막힌것 = RetryStatus(channel="C9", stuck_sec=3600.0, alert=True)
        catchup.retry_statuses = [막힌것]
        worker, _queue, _client = make_worker(database=database, catchup=catchup)

        assert worker.retry_catchup() == [막힌것]


class Test캐치업창을받는다:
    """끊겼던 시간이 기본 창보다 길면 그만큼 넓게 봐야 그 구간이 잡힌다."""

    def test_창을_주면_그대로_쓴다(self, database) -> None:
        catchup = FakeCatchup(CatchupReport(missed=[], skipped=[], unchecked_channels=[]))
        worker, _queue, _client = make_worker(database=database, catchup=catchup)

        worker.catch_up(["C1"], window_sec=9999.0)

        assert catchup.calls == [(["C1"], 9999.0)]

    def test_안_주면_설정값을_쓴다(self, database) -> None:
        catchup = FakeCatchup(CatchupReport(missed=[], skipped=[], unchecked_channels=[]))
        settings = RuntimeSettings(heartbeat_interval_sec=0.01, catchup_window_sec=777.0)
        worker, _queue, _client = make_worker(database=database, catchup=catchup, settings=settings)

        worker.catch_up(["C1"])

        assert catchup.calls == [(["C1"], 777.0)]


class 동시계측핸들러:
    """동시에 몇 건이 처리 중인지 재는 대역. 상한만큼 모이면 함께 풀어준다."""

    def __init__(self, expected: int, timeout: float = 5.0) -> None:
        self._lock = threading.Lock()
        self._expected = expected
        self._timeout = timeout
        self._gate = threading.Event()
        self.동시최대 = 0
        self.진입 = 0
        self.완료 = 0
        self._현재 = 0

    def handle(self, ctx: RequestContext) -> HandleOutcome:
        with self._lock:
            self._현재 += 1
            self.진입 += 1
            self.동시최대 = max(self.동시최대, self._현재)
            if self._현재 >= self._expected:
                self._gate.set()
        self._gate.wait(self._timeout)
        with self._lock:
            self._현재 -= 1
            self.완료 += 1
        return HandleOutcome(ok=True)


class Test동시처리상한:
    """워커 1개가 한 번에 하나씩만 처리하면 서로 다른 스레드의 요청이 서로를
    기다린다. 실측에서 최대 42분이었다. max_concurrent 를 상한으로 쓴다
    (sca-si6)."""

    #: 사건이 영영 안 오는 결함을 만나도 시험이 멈추지 않게 하는 상한이다.
    #: 통과 경로는 이 값에 닿지 않는다.
    _마감초 = 10.0

    def _세_건이_다_진입할_때까지(self, handler: Any, 건수: int) -> Callable[[], bool]:
        마감 = time.monotonic() + self._마감초
        return lambda: handler.진입 >= 건수 or time.monotonic() > 마감

    def _두_건이_다_끝날_때까지(self, handler: Any, 건수: int) -> Callable[[], bool]:
        """상한이 1이면 둘이 동시에 진입할 수 없다. 완료로 센다."""
        마감 = time.monotonic() + self._마감초
        return lambda: handler.완료 >= 건수 or time.monotonic() > 마감

    def test_상한만큼_동시에_처리한다(self, database) -> None:
        handler = 동시계측핸들러(expected=3)
        settings = RuntimeSettings(heartbeat_interval_sec=0.01, max_concurrent=3)
        worker, queue, _client = make_worker(
            database=database, handler=handler, settings=settings
        )
        for i in range(3):
            queue.enqueue(ctx(f"{i}.0", thread=f"t{i}"))
        # 폴링 횟수로 끊지 않는다. 빈 폴링이 한 번만 섞여도 세 번째 건을
        # 집기 전에 루프가 끝나 동시최대가 3에 못 미친다 (sca-2n2).
        worker.run_forever(self._세_건이_다_진입할_때까지(handler, 3))
        assert handler.동시최대 == 3

    def test_상한을_넘지_않는다(self, database) -> None:
        handler = 동시계측핸들러(expected=2, timeout=0.3)
        settings = RuntimeSettings(heartbeat_interval_sec=0.01, max_concurrent=1)
        worker, queue, _client = make_worker(
            database=database, handler=handler, settings=settings
        )
        for i in range(2):
            queue.enqueue(ctx(f"{i}.0", thread=f"t{i}"))
        worker.run_forever(self._두_건이_다_끝날_때까지(handler, 2))
        assert handler.동시최대 == 1

    def test_반환하기_전에_처리_중인_건을_기다린다(self, database) -> None:
        """안 기다리면 종료 중이던 건이 처리 표식만 남고 아무도 끝내지 않는다."""

        class 늦게끝나는핸들러:
            def __init__(self) -> None:
                self.끝났다 = False

            def handle(self, ctx: RequestContext) -> HandleOutcome:
                time.sleep(0.2)
                self.끝났다 = True
                return HandleOutcome(ok=True)

        handler = 늦게끝나는핸들러()
        settings = RuntimeSettings(heartbeat_interval_sec=0.01, max_concurrent=2)
        worker, queue, _client = make_worker(
            database=database, handler=handler, settings=settings
        )
        queue.enqueue(ctx("1.0"))
        멈춤 = [False, True]
        worker.run_forever(lambda: 멈춤.pop(0) if 멈춤 else True)
        assert handler.끝났다


class Test캐치업도_관리_명령_판정을_거친다:
    """봇이 꺼져 있는 동안 받은 !ping 이 회수된 뒤 명령이 아니라 모델 요청으로
    갔다. 소켓 경로만 AdminRouter 를 거쳤다 (sca-oyku)."""

    def _명령판정(self, tmp_path: Path, 처리할본문: set[str], 보냄: list[Any], markers=None):
        from slack_cli_agent.admin.admission import AdminAdmission
        from slack_cli_agent.admin.command import AdminResult

        class 대역라우터:
            def dispatch(self, text, ctx):
                return AdminResult(message="pong") if text in 처리할본문 else None

        return AdminAdmission(
            router=대역라우터(),
            context_builder=관리맥락(tmp_path),
            reply=lambda channel, thread_ts, message: 보냄.append(message),
            markers=markers,
        )

    def _요청(self, ts: str, text: str) -> RequestContext:
        return RequestContext(channel="C1", user="U1", ts=ts, thread_ts="1.0", text=text)

    def test_회수한_관리_명령은_큐에_안_들어간다(self, database, tmp_path: Path) -> None:
        보냄: list[Any] = []
        명령 = self._요청("1.1", "!ping")
        worker, queue, _ = make_worker(
            database=database,
            catchup=FakeCatchup(CatchupReport(missed=[명령], skipped=[], unchecked_channels=[])),
            admin=self._명령판정(tmp_path, {"!ping"}, 보냄),
        )
        report = worker.catch_up(["C1"])
        assert queue.pending() == []
        assert 보냄 == ["pong"]
        assert report.missed == []

    def test_명령이_아닌_것은_그대로_큐에_들어간다(self, database, tmp_path: Path) -> None:
        보냄: list[Any] = []
        일반 = self._요청("1.2", "오늘 일정 알려줘")
        worker, queue, _ = make_worker(
            database=database,
            catchup=FakeCatchup(CatchupReport(missed=[일반], skipped=[], unchecked_channels=[])),
            admin=self._명령판정(tmp_path, {"!ping"}, 보냄),
        )
        report = worker.catch_up(["C1"])
        assert [job.context.ts for job in queue.pending()] == ["1.2"]
        assert 보냄 == []
        assert [c.ts for c in report.missed] == ["1.2"]

    def test_회수한_관리_명령에_완료_표식을_단다(self, database, tmp_path: Path) -> None:
        """큐에 안 들어가므로 _finish 가 안 돈다. 표식이 없으면 다음 캐치업이
        같은 명령을 다시 찾아 또 실행한다 (코덱스 리뷰). 표식 자체는
        AdminAdmission 이 달아 소켓 경로와 같은 흔적이 남는다 (sca-sk9t)."""
        보냄: list[Any] = []
        client = FakeSlackClient()
        명령 = self._요청("1.1", "!ping")
        worker, _queue, _ = make_worker(
            database=database,
            catchup=FakeCatchup(CatchupReport(missed=[명령], skipped=[], unchecked_channels=[])),
            admin=self._명령판정(tmp_path, {"!ping"}, 보냄, markers=ReactionMarker(client)),
        )
        worker.catch_up(["C1"])
        assert ("add", "C1", "1.1", "white_check_mark") in client.calls

    def test_관리_명령_뒤에_묻힌_요청도_표식을_받는다(self, database, tmp_path: Path) -> None:
        """대표가 관리 명령이면 그 스레드의 묻힌 요청을 아무도 안 끝낸다."""
        보냄: list[Any] = []
        명령 = self._요청("1.5", "!ping")
        묻힘 = self._요청("1.4", "이전 질문")
        worker, _queue, client = make_worker(
            database=database,
            catchup=FakeCatchup(
                CatchupReport(missed=[명령], skipped=[묻힘], unchecked_channels=[])
            ),
            admin=self._명령판정(tmp_path, {"!ping"}, 보냄),
        )
        worker.catch_up(["C1"])
        assert ("add", "C1", "1.4", "white_check_mark") in client.calls

    def test_재시도_캐치업도_판정을_거친다(self, database, tmp_path: Path) -> None:
        """첫 조회가 실패한 뒤 재시도에서 찾은 관리 명령이 모델로 갔다."""
        보냄: list[Any] = []
        명령 = self._요청("1.6", "!ping")
        catchup = FakeCatchup(
            CatchupReport(missed=[], skipped=[], unchecked_channels=[]),
            retry_statuses=[RetryStatus(channel="C1", stuck_sec=1.0, alert=False, missed=(명령,))],
        )
        worker, queue, _ = make_worker(
            database=database, catchup=catchup, admin=self._명령판정(tmp_path, {"!ping"}, 보냄)
        )
        worker.retry_catchup()
        assert queue.pending() == []
        assert 보냄 == ["pong"]

    def test_판정이_터지면_모델로_안_넘긴다(self, database, tmp_path: Path) -> None:
        """dispatch 가 명령 일부를 이미 실행한 뒤 터질 수 있다. 모델로 넘기면
        그 부작용이 두 번 일어난다 (코덱스 리뷰)."""
        from slack_cli_agent.admin.admission import AdminAdmission

        class 터지는라우터:
            def dispatch(self, text, ctx):
                raise RuntimeError("판정 실패")

        판정 = AdminAdmission(
            router=터지는라우터(),
            context_builder=관리맥락(tmp_path),
            reply=lambda channel, thread_ts, message: None,
        )
        worker, queue, client = make_worker(
            database=database,
            catchup=FakeCatchup(
                CatchupReport(missed=[self._요청("1.7", "!ping")], skipped=[], unchecked_channels=[])
            ),
            admin=판정,
        )
        worker.catch_up(["C1"])
        assert queue.pending() == []
        assert ("add", "C1", "1.7", "x") in client.calls

    def test_판정기가_없으면_예전처럼_전부_큐에_넣는다(self, database) -> None:
        """조립이 주입을 빠뜨려도 요청이 사라지지는 않는다."""
        명령 = self._요청("1.3", "!ping")
        worker, queue, _ = make_worker(
            database=database,
            catchup=FakeCatchup(CatchupReport(missed=[명령], skipped=[], unchecked_channels=[])),
        )
        worker.catch_up(["C1"])
        assert [job.context.ts for job in queue.pending()] == ["1.3"]
