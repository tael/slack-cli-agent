"""영속 작업 큐. 원본의 인메모리 대기줄과 복구 로직을 대체한다."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import pytest

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.context import RequestContext
from slack_cli_agent.jobs.heartbeat import WorkerHeartbeat
from slack_cli_agent.jobs.ports import Job, JobQueue, JobStatus, ReclaimResult
from slack_cli_agent.jobs.queue import SqliteJobQueue
from slack_cli_agent.storage.database import Database


def ctx(ts: str, thread: str = "", channel: str = "C1") -> RequestContext:
    return RequestContext(
        channel=channel, user="U1", ts=ts, thread_ts=thread or ts, text="본문"
    )


@pytest.fixture
def queue(database) -> SqliteJobQueue:
    return SqliteJobQueue(database)


def claim(queue: SqliteJobQueue, worker: str) -> Job:
    """claim_next 가 None 을 내면 그 자리에서 실패시킨다."""
    job = queue.claim_next(worker)
    assert job is not None
    return job


def _완료(queue: SqliteJobQueue, job: Job, *, ok: bool = True) -> None:
    queue.complete(job.id, ok=ok, lease=job.lease)


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

        queue.complete(first.id, ok=True, lease=first.lease)
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
        assert [claim(queue, f"w{i}").context.ts for i in range(3)] == [
            "0.0", "1.0", "2.0"
        ]

    def test_대기_작업이_없으면_None_이다(self, queue: SqliteJobQueue) -> None:
        assert queue.claim_next("w1") is None

    def test_잡으면_시도_횟수가_증가한다(self, queue: SqliteJobQueue) -> None:
        queue.enqueue(ctx("1.1"))
        assert claim(queue, "w1").attempts == 1

    def test_실패로_끝나도_그_스레드가_풀린다(self, queue: SqliteJobQueue) -> None:
        queue.enqueue(ctx("1.1", "T1"))
        queue.enqueue(ctx("1.2", "T1"))
        first = claim(queue, "w1")
        queue.complete(first.id, ok=False, failure="엔진 오류", lease=first.lease)
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


class TestBlockedOnThread:
    """`claim_next` 가 같은 thread_ts 를 직렬화하므로, 그 스레드에 미완료 작업이
    하나라도 더 있으면 이 작업은 바로 시작하지 못한다."""

    def test_자기_혼자면_막히지_않는다(self, queue: SqliteJobQueue) -> None:
        queue.enqueue(ctx("1.1", "T1"))
        assert queue.blocked_on_thread("T1", "1.1") is False

    def test_같은_스레드에_대기_작업이_있으면_막힌다(self, queue: SqliteJobQueue) -> None:
        queue.enqueue(ctx("1.1", "T1"))
        queue.enqueue(ctx("1.2", "T1"))
        assert queue.blocked_on_thread("T1", "1.2") is True

    def test_같은_스레드에_실행중_작업이_있으면_막힌다(self, queue: SqliteJobQueue) -> None:
        queue.enqueue(ctx("1.1", "T1"))
        queue.claim_next("w1")
        queue.enqueue(ctx("1.2", "T1"))
        assert queue.blocked_on_thread("T1", "1.2") is True

    def test_다른_스레드의_작업은_막지_않는다(self, queue: SqliteJobQueue) -> None:
        queue.enqueue(ctx("1.1", "T1"))
        queue.enqueue(ctx("2.1", "T2"))
        assert queue.blocked_on_thread("T2", "2.1") is False

    def test_끝난_작업은_막지_않는다(self, queue: SqliteJobQueue) -> None:
        queue.enqueue(ctx("1.1", "T1"))
        first = queue.claim_next("w1")
        assert first is not None
        queue.complete(first.id, ok=True, lease=first.lease)
        queue.enqueue(ctx("1.2", "T1"))
        assert queue.blocked_on_thread("T1", "1.2") is False


class TestPersistence:
    def test_재기동해도_대기_작업이_남는다(self, database) -> None:
        SqliteJobQueue(database).enqueue(ctx("1.1"))
        # 같은 DB 파일을 다시 여는 것이 프로세스 재기동에 대응한다
        assert [j.context.ts for j in SqliteJobQueue(database).pending()] == ["1.1"]

    def test_실행_중_되돌리면_다시_나온다(self, queue: SqliteJobQueue) -> None:
        queue.enqueue(ctx("1.1"))
        job = claim(queue, "w1")
        queue.requeue(job.id, lease=job.lease)
        assert queue.claim_next("w2") is not None

    def test_완료된_작업은_다시_안_나온다(self, queue: SqliteJobQueue) -> None:
        queue.enqueue(ctx("1.1"))
        _완료(queue, claim(queue, "w1"))
        assert queue.claim_next("w2") is None
        assert queue.counts() == {JobStatus.COMPLETED.value: 1}

    def test_끝난_지_오래된_작업을_지운다(self, queue: SqliteJobQueue) -> None:
        queue.enqueue(ctx("1.1"))
        _완료(queue, claim(queue, "w1"))
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
        job = claim(queue, "w1")
        queue.heartbeat(job.id, job.lease)
        assert queue.reclaim_stale(deadline=time.time() - 1, max_attempts=3).total == 0


class Test소유권검증:
    """회수된 작업은 다음 claim 이 새 시도를 연다. 그 사이 옛 워커가 돌아와
    결과를 쓰면 지금 도는 시도의 상태를 덮는다."""

    def test_회수된_뒤_옛_워커의_완료는_안_먹힌다(self, queue: SqliteJobQueue) -> None:
        queue.enqueue(ctx("1.1"))
        옛시도 = claim(queue, "죽은워커")
        queue.reclaim_stale(deadline=time.time() + 1, max_attempts=3)
        새시도 = claim(queue, "산워커")

        queue.complete(옛시도.id, True, "", lease=옛시도.lease)

        # 완료로 굳었으면 회수 대상에서 빠진다. 아직 도는 중이어야 한다.
        assert queue.reclaim_stale(deadline=time.time() + 1, max_attempts=9).total == 1
        assert 새시도.attempts != 옛시도.attempts

    def test_지금_도는_시도의_완료는_먹힌다(self, queue: SqliteJobQueue) -> None:
        queue.enqueue(ctx("1.1"))
        job = claim(queue, "w1")

        queue.complete(job.id, True, "", lease=job.lease)

        assert queue.reclaim_stale(deadline=time.time() + 1, max_attempts=9).total == 0

    def test_회수된_뒤_옛_워커의_박동은_정체_판정을_늦추지_못한다(
        self, database: Database
    ) -> None:
        """시계를 직접 쥔다. 옛 박동이 먹히면 판정 시점이 그만큼 밀린다."""
        시각 = [1000.0]
        queue = SqliteJobQueue(database, now=lambda: 시각[0])
        queue.enqueue(ctx("1.1"))
        옛시도 = claim(queue, "죽은워커")
        시각[0] = 1100.0
        queue.reclaim_stale(deadline=1050.0, max_attempts=9)
        claim(queue, "산워커")

        시각[0] = 1200.0
        queue.heartbeat(옛시도.id, lease=옛시도.lease)

        assert queue.reclaim_stale(deadline=1150.0, max_attempts=9).total == 1


    def test_회수된_뒤_옛_워커의_되돌리기는_안_먹힌다(self, queue: SqliteJobQueue) -> None:
        """종료 중인 옛 워커가 지금 도는 시도를 대기로 되돌리면, 또 다른
        워커가 같은 요청을 겹쳐 집는다."""
        queue.enqueue(ctx("1.1"))
        옛시도 = claim(queue, "죽은워커")
        queue.reclaim_stale(deadline=time.time() + 1, max_attempts=9)
        claim(queue, "산워커")

        queue.requeue(옛시도.id, lease=옛시도.lease)

        assert queue.claim_next("또다른워커") is None

    def test_지금_도는_시도의_되돌리기는_먹힌다(self, queue: SqliteJobQueue) -> None:
        queue.enqueue(ctx("1.1"))
        job = claim(queue, "w1")

        queue.requeue(job.id, lease=job.lease)

        assert queue.claim_next("w2") is not None

    def test_안_먹힌_완료는_거짓을_돌려준다(self, queue: SqliteJobQueue) -> None:
        """워커가 이 값을 보고 슬랙 표식을 달지 말지 정한다."""
        queue.enqueue(ctx("1.1"))
        옛시도 = claim(queue, "죽은워커")
        queue.reclaim_stale(deadline=time.time() + 1, max_attempts=9)
        새시도 = claim(queue, "산워커")

        assert queue.complete(옛시도.id, True, "", lease=옛시도.lease) is False
        assert queue.complete(새시도.id, True, "", lease=새시도.lease) is True


    def test_행을_비운_뒤_id가_재사용돼도_옛_결과가_안_먹힌다(
        self, queue: SqliteJobQueue
    ) -> None:
        """jobs.id 는 rowid 라 지운 뒤 다시 쓰인다. 시도 번호도 0부터라
        (id, 시도) 만으로는 다른 작업을 같은 작업으로 본다."""
        queue.enqueue(ctx("1.1"))
        옛작업 = claim(queue, "오래된워커")
        queue.complete(옛작업.id, True, "", lease=옛작업.lease)
        queue.purge_finished(before=time.time() + 1)

        queue.enqueue(ctx("2.2"))
        새작업 = claim(queue, "지금워커")
        assert 새작업.id == 옛작업.id, "이 시험은 id 재사용을 전제로 한다"

        assert queue.complete(옛작업.id, True, "", lease=옛작업.lease) is False
        assert queue.complete(새작업.id, True, "", lease=새작업.lease) is True


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


class Test실패건재등록:
    """실패로 끝난 건을 다시 등록할 수 있는가.

    `UNIQUE(channel, message_ts)` 와 `INSERT OR IGNORE` 때문에, 실패한 행이
    그 키를 계속 차지한다. 캐치업이 미응답 멘션을 찾아내도 재등록이 조용히
    무시돼 그 요청은 영영 처리되지 않는다.

    완료된 건은 반대다. 다시 등록하면 같은 답이 두 번 나간다 — 그 차단은
    그대로 둔다.
    """

    def test_실패한건은_다시_등록된다(self, database) -> None:
        queue = SqliteJobQueue(database)
        assert queue.enqueue(ctx("1.0")) is True
        job = queue.claim_next("w")
        assert job is not None
        queue.complete(job.id, ok=False, failure="엔진 오류", lease=job.lease)

        assert queue.enqueue(ctx("1.0")) is True
        assert [j.context.ts for j in queue.pending()] == ["1.0"]

    def test_재등록해도_실패기록은_지워진다(self, database) -> None:
        """앞 회차의 실패 사유가 남아 있으면 지금 상태를 잘못 읽는다."""
        queue = SqliteJobQueue(database)
        queue.enqueue(ctx("1.0"))
        job = queue.claim_next("w")
        assert job is not None
        queue.complete(job.id, ok=False, failure="엔진 오류", lease=job.lease)
        queue.enqueue(ctx("1.0"))
        assert queue.counts().get(JobStatus.FAILED.value, 0) == 0

    def test_완료된건은_다시_등록되지_않는다(self, database) -> None:
        queue = SqliteJobQueue(database)
        queue.enqueue(ctx("1.0"))
        job = queue.claim_next("w")
        assert job is not None
        queue.complete(job.id, ok=True, lease=job.lease)

        assert queue.enqueue(ctx("1.0")) is False
        assert queue.pending() == []

    def test_시도상한을_넘긴건은_다시_등록되지_않는다(self, database) -> None:
        """같은 요청이 계속 실패하는데 캐치업이 매번 되살리면 끝나지 않는다."""
        queue = SqliteJobQueue(database)
        queue.enqueue(ctx("1.0"))
        for _ in range(3):
            job = queue.claim_next("w")
            assert job is not None
            queue.complete(job.id, ok=False, failure="엔진 오류", lease=job.lease)
            queue.enqueue(ctx("1.0"), max_attempts=3)
        assert queue.enqueue(ctx("1.0"), max_attempts=3) is False

    def test_대기중인_같은건은_그대로_무시된다(self, database) -> None:
        queue = SqliteJobQueue(database)
        assert queue.enqueue(ctx("1.0")) is True
        assert queue.enqueue(ctx("1.0")) is False
        assert len(queue.pending()) == 1

    def test_실행중인_같은건은_그대로_무시된다(self, database) -> None:
        """처리 중인 건을 대기로 되돌리면 같은 답이 두 번 나간다."""
        queue = SqliteJobQueue(database)
        queue.enqueue(ctx("1.0"))
        queue.claim_next("w")
        assert queue.enqueue(ctx("1.0")) is False
        assert queue.pending() == []
