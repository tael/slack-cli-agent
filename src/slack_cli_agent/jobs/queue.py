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

    def enqueue(self, ctx: RequestContext, max_attempts: int = 0) -> bool:
        """작업을 등록한다. 새로 들어갔으면 True.

        `(channel, message_ts)` 가 유일해 같은 메시지는 한 번만 들어간다. 그
        차단이 대기·실행·완료에는 맞지만 실패에는 안 맞는다 — 실패한 행이 그
        키를 계속 차지하면, 되짚기가 미응답 멘션을 찾아내도 재등록이 조용히
        무시돼 그 요청은 영영 처리되지 않는다. 실패 행은 대기로 되돌린다.

        `max_attempts` 가 0 보다 크면 그만큼 시도한 건은 되실행하지 않는다.
        같은 요청이 계속 실패하는데 되짚기가 매번 되살리면 끝나지 않는다.
        """
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
            if cursor.rowcount > 0:
                return True
            return self._reopen_failed(conn, ctx, max_attempts)

    def _reopen_failed(self, conn: sqlite3.Connection, ctx: RequestContext, max_attempts: int) -> bool:
        """실패로 끝난 같은 건을 대기로 되돌린다. 되돌렸으면 True.

        앞 회차의 실패 사유와 워커·시각을 함께 지운다. 남겨 두면 대기 중인
        작업이 실패 사유를 달고 있어 상태 조회가 지금 상태를 잘못 읽는다.
        `attempts` 는 그대로 둔다 — 그 값이 상한 판정의 근거다.
        """
        condition = "status = ? AND channel = ? AND message_ts = ?"
        params: list[object] = [JobStatus.FAILED.value, ctx.channel, ctx.ts]
        if max_attempts > 0:
            condition += " AND attempts < ?"
            params.append(max_attempts)
        cursor = conn.execute(
            f"""
            UPDATE jobs
               SET status = ?, payload = ?, worker_id = NULL, started_at = NULL,
                   heartbeat_ts = NULL, finished_at = NULL, failure = NULL
             WHERE {condition}
            """,
            [JobStatus.QUEUED.value, ctx.to_json(), *params],
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
