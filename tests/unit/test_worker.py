"""큐 소비 워커.

Worker 는 JobQueue 와 RequestHandler 를 잇는다. 처리기 자체는 대역으로
세우고, 이 시험은 워커가 큐 상태 전이·리액션 표식·되짚기 중복 제거를
정확히 하는지만 본다.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.context import RequestContext
from slack_cli_agent.core.ports import HandleOutcome
from slack_cli_agent.jobs.heartbeat import WorkerHeartbeat
from slack_cli_agent.jobs.queue import SqliteJobQueue
from slack_cli_agent.reliability.catchup import CatchupReport
from slack_cli_agent.slack.reactions import ReactionMarker


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

    retry_statuses: list = field(default_factory=list)
    retry_calls: int = 0

    def sweep(self, channels: list[str], window: float) -> CatchupReport:
        self.calls.append((channels, window))
        return self.report

    def retry_pending(self) -> list:
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

    result: object

    def reclaim_stale(self, deadline: float, max_attempts: int):
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
        from slack_cli_agent.jobs.ports import ReclaimResult

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
            queue=fake_queue,
            handler=FakeHandler(),
            heartbeat=heartbeat,
            catchup=FakeCatchup(CatchupReport(missed=[], skipped=[], unchecked_channels=[])),
            markers=markers,
            settings=settings,
        )

        result = worker.reclaim()

        assert result.requeued == [requeued_ctx]
        assert result.failed == [failed_ctx]
        assert ("remove", "C1", "1.1", "eyes") in client.calls
        assert ("add", "C1", "2.1", "x") in client.calls


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

        release.set()
        thread.join(timeout=2)


class Test대표건이_걸러져도_묻힌_건은_따라간다:
    def test_이미_대기중인_대표건에_묻힌_건도_같은_표식을_받는다(self, database) -> None:
        """대표건을 거르는 것과 묻힌 건을 버리는 것은 다르다.

        접수 경로로 이미 큐에 들어간 요청이 되짚기에도 잡히면 대표건은
        걸러진다. 그때 묻힌 건을 그냥 버리면 표식이 안 달려, 다음 되짚기가
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


class Test되짚기가_실패건을_되살린다:
    """실패로 끝난 건을 되짚기가 다시 등록하는가.

    실패한 행이 `(channel, message_ts)` 를 계속 차지하면, 되짚기가 미응답
    멘션을 찾아내도 등록이 조용히 무시된다. 그 요청은 답을 못 받은 채로
    영영 남는다.
    """

    def test_실패한_대표건은_다시_등록된다(self, database) -> None:
        worker, queue, _client = make_worker(database=database)
        queue.enqueue(ctx("1.1", "T1"))
        job = queue.claim_next("w")
        assert job is not None
        queue.complete(job.id, ok=False, failure="엔진 오류")

        report = CatchupReport(missed=[ctx("1.1", "T1")], skipped=[], unchecked_channels=[])
        worker._catchup = FakeCatchup(report)
        accepted = worker.catch_up(["C1"])

        assert [c.ts for c in accepted.missed] == ["1.1"]
        assert [j.context.ts for j in queue.pending()] == ["1.1"]

    def test_시도상한을_넘긴건은_되살리지_않는다(self, database) -> None:
        """계속 실패하는 요청을 되짚기가 매번 되살리면 끝나지 않는다."""
        settings = RuntimeSettings(heartbeat_interval_sec=0.01, job_max_attempts=1)
        worker, queue, _client = make_worker(database=database, settings=settings)
        queue.enqueue(ctx("1.1", "T1"))
        job = queue.claim_next("w")
        assert job is not None
        queue.complete(job.id, ok=False, failure="엔진 오류")

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
        queue.complete(job.id, ok=True)

        report = CatchupReport(missed=[ctx("1.1", "T1")], skipped=[], unchecked_channels=[])
        worker._catchup = FakeCatchup(report)
        assert worker.catch_up(["C1"]).missed == []


class Test마치지못한되짚기를다시본다:
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


class Test되짚기창을받는다:
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
