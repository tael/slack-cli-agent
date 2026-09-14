"""작업 큐의 SQLite 구현. 계약은 ports.JobQueue 에 있다."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable

from ..core.context import RequestContext
from ..storage.database import Database
from ..storage.repository import SqliteRepository
from .ports import Job, JobStatus, ReclaimResult


class SqliteJobQueue(SqliteRepository):
    def __init__(self, db: Database, now: Callable[[], float] = time.time) -> None:
        super().__init__(db)
        self._now = now

    def enqueue(self, ctx: RequestContext) -> bool:
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO jobs
                  (channel, thread_ts, message_ts, user_id, payload,
                   status, created_at, attempts)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (ctx.channel, ctx.thread_ts, ctx.ts, ctx.user,
                 ctx.to_json(), JobStatus.QUEUED.value, self._now()),
            )
            return cursor.rowcount > 0

    def claim_next(self, worker_id: str) -> Job | None:
        now = self._now()
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT id, payload, attempts, created_at FROM jobs
                 WHERE status = ?
                   AND thread_ts NOT IN (
                       SELECT thread_ts FROM jobs WHERE status = ?
                   )
                 ORDER BY created_at, id
                 LIMIT 1
                """,
                (JobStatus.QUEUED.value, JobStatus.RUNNING.value),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE jobs
                   SET status = ?, worker_id = ?, started_at = ?,
                       heartbeat_ts = ?, attempts = attempts + 1
                 WHERE id = ?
                """,
                (JobStatus.RUNNING.value, worker_id, now, now, row["id"]),
            )
        return self._to_job(row, attempts_delta=1)

    def heartbeat(self, job_id: int) -> None:
        self._execute(
            "UPDATE jobs SET heartbeat_ts = ? WHERE id = ? AND status = ?",
            (self._now(), job_id, JobStatus.RUNNING.value),
        )

    def complete(self, job_id: int, ok: bool, failure: str = "") -> None:
        status = JobStatus.COMPLETED if ok else JobStatus.FAILED
        self._execute(
            "UPDATE jobs SET status = ?, finished_at = ?, failure = ? WHERE id = ?",
            (status.value, self._now(), failure, job_id),
        )

    def requeue(self, job_id: int) -> None:
        self._execute(
            """
            UPDATE jobs SET status = ?, worker_id = NULL,
                   started_at = NULL, heartbeat_ts = NULL
             WHERE id = ? AND status = ?
            """,
            (JobStatus.QUEUED.value, job_id, JobStatus.RUNNING.value),
        )

    def reclaim_stale(self, deadline: float, max_attempts: int) -> ReclaimResult:
        requeued: list[RequestContext] = []
        failed: list[RequestContext] = []

        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT id, payload, attempts FROM jobs
                 WHERE status = ?
                   AND (heartbeat_ts IS NULL OR heartbeat_ts < ?)
                """,
                (JobStatus.RUNNING.value, deadline),
            ).fetchall()

            for row in rows:
                context = RequestContext.from_json(row["payload"])
                if int(row["attempts"]) >= max_attempts:
                    conn.execute(
                        "UPDATE jobs SET status = ?, finished_at = ?, failure = ?"
                        " WHERE id = ?",
                        (JobStatus.FAILED.value, self._now(),
                         "워커 응답 없음. 재시도 상한 초과", row["id"]),
                    )
                    failed.append(context)
                else:
                    conn.execute(
                        """
                        UPDATE jobs SET status = ?, worker_id = NULL,
                               started_at = NULL, heartbeat_ts = NULL
                         WHERE id = ?
                        """,
                        (JobStatus.QUEUED.value, row["id"]),
                    )
                    requeued.append(context)

        return ReclaimResult(requeued=requeued, failed=failed)

    def pending(self, limit: int = 50) -> list[Job]:
        rows = self._fetch_all(
            """
            SELECT id, payload, attempts, created_at FROM jobs
             WHERE status = ? ORDER BY created_at, id LIMIT ?
            """,
            (JobStatus.QUEUED.value, limit),
        )
        return [self._to_job(row) for row in rows]

    def counts(self) -> dict[str, int]:
        rows = self._fetch_all("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
        return {row["status"]: int(row["n"]) for row in rows}

    def purge_finished(self, before: float) -> int:
        cursor = self._execute(
            "DELETE FROM jobs WHERE status IN (?, ?) AND finished_at < ?",
            (JobStatus.COMPLETED.value, JobStatus.FAILED.value, before),
        )
        return cursor.rowcount

    @staticmethod
    def _to_job(row: sqlite3.Row, attempts_delta: int = 0) -> Job:
        return Job(
            id=int(row["id"]),
            context=RequestContext.from_json(row["payload"]),
            attempts=int(row["attempts"]) + attempts_delta,
            created_at=float(row["created_at"]),
        )
