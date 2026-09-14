"""영속 작업 큐. 원본의 인메모리 대기줄과 복구 로직을 대체한다."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import pytest

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.context import RequestContext
from slack_cli_agent.jobs.heartbeat import WorkerHeartbeat
from slack_cli_agent.jobs.ports import JobQueue, JobStatus, ReclaimResult
from slack_cli_agent.jobs.queue import SqliteJobQueue


def ctx(ts: str, thread: str = "", channel: str = "C1") -> RequestContext:
    return RequestContext(
        channel=channel, user="U1", ts=ts, thread_ts=thread or ts, text="본문"
    )


@pytest.fixture
def queue(database) -> SqliteJobQueue:
    return SqliteJobQueue(database)


class TestContract:
    def test_구현이_큐_계약을_만족한다(self, queue: SqliteJobQueue) -> None:
        assert isinstance(queue, JobQueue)


class TestEnqueue:
    def test_같은_메시지를_두_번_등록하지_않는다(self, queue: SqliteJobQueue) -> None:
        assert queue.enqueue(ctx("1.1")) is True
        assert queue.enqueue(ctx("1.1")) is False

    def test_채널이_다르면_같은_ts_도_별개다(self, queue: SqliteJobQueue) -> None:
        assert queue.enqueue(ctx("1.1", channel="C1")) is True
        assert queue.enqueue(ctx("1.1", channel="C2")) is True

    def test_등록한_맥락이_그대로_복원된다(self, queue: SqliteJobQueue) -> None:
        original = ctx("1.1")
        queue.enqueue(original)
        assert queue.pending()[0].context == original


class TestClaim:
    def test_같은_스레드는_동시에_실행되지_않는다(self, queue: SqliteJobQueue) -> None:
        queue.enqueue(ctx("1.1", "T1"))
        queue.enqueue(ctx("1.2", "T1"))

        first = queue.claim_next("w1")
        assert first is not None and first.context.ts == "1.1"
        assert queue.claim_next("w2") is None

        queue.complete(first.id, ok=True)
        second = queue.claim_next("w2")
        assert second is not None and second.context.ts == "1.2"

    def test_다른_스레드는_동시에_실행된다(self, queue: SqliteJobQueue) -> None:
        queue.enqueue(ctx("1.1", "T1"))
        queue.enqueue(ctx("2.1", "T2"))
        assert queue.claim_next("w1") is not None
        assert queue.claim_next("w2") is not None

    def test_등록_순서대로_나온다(self, queue: SqliteJobQueue) -> None:
        for index in range(3):
            queue.enqueue(ctx(f"{index}.0", f"T{index}"))
            time.sleep(0.001)
        assert [queue.claim_next(f"w{i}").context.ts for i in range(3)] == [
            "0.0", "1.0", "2.0"
        ]

    def test_대기_작업이_없으면_None_이다(self, queue: SqliteJobQueue) -> None:
        assert queue.claim_next("w1") is None

    def test_잡으면_시도_횟수가_증가한다(self, queue: SqliteJobQueue) -> None:
        queue.enqueue(ctx("1.1"))
        assert queue.claim_next("w1").attempts == 1

    def test_실패로_끝나도_그_스레드가_풀린다(self, queue: SqliteJobQueue) -> None:
        queue.enqueue(ctx("1.1", "T1"))
        queue.enqueue(ctx("1.2", "T1"))
        first = queue.claim_next("w1")
        queue.complete(first.id, ok=False, failure="엔진 오류")
        assert queue.claim_next("w2") is not None

    def test_여러_워커가_동시에_불러도_한_번만_나온다(self, database) -> None:
        SqliteJobQueue(database).enqueue(ctx("1.1"))
        claimed: list[object] = []
        barrier = threading.Barrier(4)

        def worker(name: str) -> None:
            queue = SqliteJobQueue(database)
            barrier.wait()
            job = queue.claim_next(name)
            if job is not None:
                claimed.append(job)

        threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(claimed) == 1


class TestPersistence:
    def test_재기동해도_대기_작업이_남는다(self, database) -> None:
        SqliteJobQueue(database).enqueue(ctx("1.1"))
        # 같은 DB 파일을 다시 여는 것이 프로세스 재기동에 대응한다
        assert [j.context.ts for j in SqliteJobQueue(database).pending()] == ["1.1"]

    def test_실행_중_되돌리면_다시_나온다(self, queue: SqliteJobQueue) -> None:
        queue.enqueue(ctx("1.1"))
        job = queue.claim_next("w1")
        queue.requeue(job.id)
        assert queue.claim_next("w2") is not None

    def test_완료된_작업은_다시_안_나온다(self, queue: SqliteJobQueue) -> None:
        queue.enqueue(ctx("1.1"))
        queue.complete(queue.claim_next("w1").id, ok=True)
        assert queue.claim_next("w2") is None
        assert queue.counts() == {JobStatus.COMPLETED.value: 1}

    def test_끝난_지_오래된_작업을_지운다(self, queue: SqliteJobQueue) -> None:
        queue.enqueue(ctx("1.1"))
        queue.complete(queue.claim_next("w1").id, ok=True)
        assert queue.purge_finished(before=time.time() + 1) == 1
        assert queue.counts() == {}


class TestReclaim:
    def test_갱신이_멈춘_작업은_대기로_되돌아간다(self, queue: SqliteJobQueue) -> None:
        queue.enqueue(ctx("1.1"))
        queue.claim_next("죽은워커")

        result = queue.reclaim_stale(deadline=time.time() + 1, max_attempts=3)

        assert [c.ts for c in result.requeued] == ["1.1"]
        assert queue.claim_next("산워커") is not None

    def test_재시도_상한을_넘으면_실패로_둔다(self, queue: SqliteJobQueue) -> None:
        queue.enqueue(ctx("1.1"))
        queue.claim_next("죽은워커")

        result = queue.reclaim_stale(deadline=time.time() + 1, max_attempts=1)

        assert [c.ts for c in result.failed] == ["1.1"]
        assert queue.claim_next("산워커") is None

    def test_갱신한_작업은_정체로_보지_않는다(self, queue: SqliteJobQueue) -> None:
        queue.enqueue(ctx("1.1"))
        job = queue.claim_next("w1")
        queue.heartbeat(job.id)
        assert queue.reclaim_stale(deadline=time.time() - 1, max_attempts=3).total == 0


@dataclass
class FakeQueue:
    """호출 인자만 기록하는 대역. Protocol 경계 덕에 DB 없이 검증한다."""

    calls: list[tuple[float, int]] = field(default_factory=list)
    result: ReclaimResult = field(
        default_factory=lambda: ReclaimResult(requeued=[], failed=[])
    )

    def reclaim_stale(self, deadline: float, max_attempts: int) -> ReclaimResult:
        self.calls.append((deadline, max_attempts))
        return self.result


class TestWorkerHeartbeat:
    def test_정체_기준을_설정값으로_계산한다(self) -> None:
        fake = FakeQueue()
        settings = RuntimeSettings(heartbeat_stale_sec=15, job_max_attempts=3)

        WorkerHeartbeat(fake, settings, now=lambda: 1000.0).reclaim_stale()

        assert fake.calls == [(985.0, 3)]

    def test_되돌린_작업의_맥락을_그대로_전달한다(self) -> None:
        target = ctx("1.1")
        fake = FakeQueue(result=ReclaimResult(requeued=[target], failed=[]))

        result = WorkerHeartbeat(fake, RuntimeSettings()).reclaim_stale()

        assert result.requeued == [target]
