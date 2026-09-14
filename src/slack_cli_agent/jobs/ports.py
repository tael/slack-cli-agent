"""작업 큐의 계약.

구현이 아니라 여기가 계약이다. 호출부는 이 Protocol 에만 의존한다. SQLite 고유
동작은 구현의 수단이고, 지켜야 할 것은 아래 docstring 이 정한다. 계약이 지켜지는
지는 tests/unit/test_jobs.py 가 검증한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from ..core.context import RequestContext


class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class Job:
    id: int
    context: RequestContext
    attempts: int
    created_at: float


@dataclass(frozen=True)
class ReclaimResult:
    """정체 작업 처리 결과. 호출부가 리액션 표식을 되돌리는 데 쓴다."""

    requeued: list[RequestContext]
    failed: list[RequestContext]

    @property
    def total(self) -> int:
        return len(self.requeued) + len(self.failed)


@runtime_checkable
class JobQueue(Protocol):
    def enqueue(self, ctx: RequestContext) -> bool:
        """등록하면 True, 같은 채널·메시지가 이미 있으면 False.

        중복 방어가 여기 있다. 프로세스 수명과 무관하게 유지된다.
        """

    def claim_next(self, worker_id: str) -> Job | None:
        """실행 중인 작업이 없는 스레드에서 가장 오래된 대기 작업을 잡는다.

        계약 —
        - 같은 `thread_ts` 의 작업은 동시에 나오지 않는다. 선행 작업이 완료나
          실패로 끝나야 다음이 나온다
        - 여러 워커가 동시에 불러도 같은 작업을 두 번 내주지 않는다
        - 잡은 작업은 RUNNING 이 되고 attempts 가 1 증가한다
        - 대기 작업이 없으면 None
        """

    def heartbeat(self, job_id: int) -> None:
        """실행 중임을 갱신한다. 갱신이 멈추면 정체 작업으로 처리된다."""

    def complete(self, job_id: int, ok: bool, failure: str = "") -> None:
        """실행을 끝낸다. 그 스레드의 다음 작업이 나올 수 있게 된다."""

    def requeue(self, job_id: int) -> None:
        """실행을 취소하고 대기로 되돌린다. 종료 중 남은 작업에 쓴다."""

    def reclaim_stale(self, deadline: float, max_attempts: int) -> ReclaimResult:
        """갱신이 `deadline` 이전에 멈춘 실행 중 작업을 처리한다.

        `max_attempts` 미만이면 대기로 되돌리고, 이상이면 실패로 둔다.
        조회와 상태 전이가 한 트랜잭션이어야 한다.
        """

    def pending(self, limit: int = 50) -> list[Job]:
        """대기 작업 목록. 등록 순서대로."""

    def counts(self) -> dict[str, int]:
        """상태별 건수. 상태 조회 명령이 쓴다."""

    def purge_finished(self, before: float) -> int:
        """끝난 지 오래된 작업을 지운다. 지운 건수를 돌려준다."""
