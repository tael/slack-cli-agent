"""Job queue contract. Callers depend only on this Protocol, not on any
particular implementation. Verified by tests/unit/test_jobs.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from ..core.context import RequestContext


class JobStatus(StrEnum):
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
    """Outcome of reclaiming stale jobs; callers use it to undo reaction marks."""

    requeued: list[RequestContext]
    failed: list[RequestContext]

    @property
    def total(self) -> int:
        return len(self.requeued) + len(self.failed)


@runtime_checkable
class StaleJobReclaimer(Protocol):
    """The one method WorkerHeartbeat needs. JobQueue satisfies it.

    Declared separately so the heartbeat isn't typed against the whole queue
    surface it never touches.
    """

    def reclaim_stale(self, deadline: float, max_attempts: int) -> ReclaimResult: ...


@runtime_checkable
class JobQueue(Protocol):
    def enqueue(self, ctx: RequestContext, max_attempts: int = 0) -> bool:
        """True if newly inserted, False if this channel/message already has a job.

        A previously failed job for the same key is the exception: it gets
        reopened to queued and returns True, so a retried mention isn't
        silently dropped. `max_attempts`, when positive, caps how many failed
        attempts get reopened this way.
        """

    def claim_next(self, worker_id: str) -> Job | None:
        """Claim the oldest queued job on a thread with no job running.

        Contract:
        - never two jobs running on the same thread_ts at once
        - never hands the same job to two concurrent workers
        - claimed job becomes RUNNING with attempts incremented by 1
        - None if nothing is queued
        """

    def heartbeat(self, job_id: int) -> None:
        """Mark the job as still alive. Stale heartbeats get reclaimed."""

    def complete(self, job_id: int, ok: bool, failure: str = "") -> None:
        """Finish the job, unblocking the next one queued on its thread."""

    def requeue(self, job_id: int) -> None:
        """Cancel a running job back to queued, e.g. on shutdown."""

    def reclaim_stale(self, deadline: float, max_attempts: int) -> ReclaimResult:
        """Handle running jobs whose heartbeat is older than `deadline`.

        Requeues if under max_attempts, otherwise marks failed. The lookup and
        the state transition must happen in one transaction.
        """

    def blocked_on_thread(self, thread_ts: str, message_ts: str) -> bool:
        """Whether another unfinished job would run before this one.

        Mirrors `claim_next`'s serialization: a job can't be claimed while
        any other QUEUED or RUNNING job shares its thread_ts. Callers use
        this to tell "waiting on an earlier request" apart from "about to
        start", which are otherwise indistinguishable at enqueue time.
        """

    def pending(self, limit: int = 50) -> list[Job]:
        """Queued jobs, oldest first."""

    def counts(self) -> dict[str, int]:
        """Job count per status."""

    def purge_finished(self, before: float) -> int:
        """Delete old finished jobs, returning how many were removed."""
