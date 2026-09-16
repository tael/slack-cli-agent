"""Watch job queue: makes "I'll watch and report back" promises actually happen.

`guard/watch.py`'s `WatchPromiseGuard` extracts what to watch for from a
`[[WATCH: ...]]` tag; this queue is what actually follows up on it, persisted
so it survives a restart. Registration-time context (`msg_ts`, `trust`,
`extra`) is stored up front since the check runs later in a different process
and can't recompute values tied to that original request.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..auth.principal import TrustLevel
from ..storage.database import Database
from ..storage.repository import SqliteRepository

log = logging.getLogger(__name__)

_SELECT_COLUMNS = (
    "id, channel, thread_ts, condition, created_at, last_run, "
    "msg_ts, checks, trust_level, extra, workdir, run_id"
)


@dataclass(frozen=True)
class WatchJob:
    id: int
    channel: str
    thread_ts: str
    condition: str
    created_at: float
    last_run: float | None
    msg_ts: str = ""
    """Message carrying the watch-mark reaction; unmarked when the job completes."""
    checks: int = 0
    trust: TrustLevel = TrustLevel.GENERAL
    """Trust level to run the check prompt with, inherited from the requester at registration time."""
    extra: Mapping[str, Any] = field(default_factory=dict)
    """Plugin-owned data; org-specific values stay out of the core fields."""
    workdir: str = ""
    """Absolute working directory at registration time. Empty on rows written
    before this was recorded, which leaves the caller on its own default."""
    run_id: str = ""
    """Identifies this job's result file. Empty means the caller pinned none."""


@runtime_checkable
class WatchJobPort(Protocol):
    def enqueue(
        self,
        channel: str,
        thread_ts: str,
        condition: str,
        msg_ts: str = "",
        trust: TrustLevel = TrustLevel.GENERAL,
        extra: Mapping[str, Any] | None = None,
        workdir: str = "",
        run_id: str = "",
    ) -> int:
        """Registers a watch job and returns its row id."""

    def due(self, now: float, min_gap: float, max_checks: int | None = None) -> list[WatchJob]:
        """Jobs due for a check. Excludes jobs checked within `min_gap` (each
        check re-invokes the engine, so this throttles frequency) and jobs at
        `max_checks` (those must be picked up by `expired` with the same
        limit, or they'd be stuck forever)."""

    def mark_checked(self, job_id: int, at: float) -> None:
        """Updates `last_run` and increments the check count."""

    def mark_polled(self, job_id: int, at: float) -> None:
        """Updates `last_run` only. A round that read the exit status and found
        the work still running never asked the engine, and counting it would
        spend the check limit on turns that cost nothing."""

    def mark_done(self, job_id: int) -> None:
        """Marks a job complete, excluding it from `due` and `expired`."""

    def expired(self, now: float, max_age: float, max_checks: int | None = None) -> list[WatchJob]:
        """Jobs to give up on: past `max_age` or at the check limit. Alerting
        an owner about these is the caller's responsibility."""


class WatchJobQueue(SqliteRepository):
    def __init__(self, db: Database, now: Callable[[], float] = time.time) -> None:
        super().__init__(db)
        self._now = now

    def enqueue(
        self,
        channel: str,
        thread_ts: str,
        condition: str,
        msg_ts: str = "",
        trust: TrustLevel = TrustLevel.GENERAL,
        extra: Mapping[str, Any] | None = None,
        workdir: str = "",
        run_id: str = "",
    ) -> int:
        cursor = self._execute(
            "INSERT INTO watch_jobs (channel, thread_ts, condition, created_at, "
            "last_run, done, msg_ts, checks, trust_level, extra, workdir, run_id) "
            "VALUES (?, ?, ?, ?, NULL, 0, ?, 0, ?, ?, ?, ?) "
            "ON CONFLICT DO NOTHING",
            (
                channel, thread_ts, condition, self._now(),
                msg_ts, int(trust), _dump_extra(extra), workdir, run_id,
            ),
        )
        if cursor.rowcount:
            assert cursor.lastrowid is not None  # sqlite always sets this on a successful INSERT
            return cursor.lastrowid
        # The partial unique index rejected it: this message already has an
        # active watch. A rerun after a crash must not add a second one, which
        # would report twice (sca-efe).
        existing = self._fetch_one(
            "SELECT id FROM watch_jobs WHERE done = 0 AND channel = ? AND msg_ts = ?",
            (channel, msg_ts),
        )
        if existing is None:  # pragma: no cover - the index is the only rejection path
            raise RuntimeError(f"감시 작업을 넣지도 찾지도 못했다 : {channel} {msg_ts}")
        log.info("이 메시지에는 활성 감시가 이미 있다. 그것을 그대로 쓴다 : %s %s", channel, msg_ts)
        return int(existing["id"])

    def due(self, now: float, min_gap: float, max_checks: int | None = None) -> list[WatchJob]:
        sql = (
            f"SELECT {_SELECT_COLUMNS} FROM watch_jobs WHERE done = 0 "
            "AND (? - COALESCE(last_run, created_at)) >= ?"
        )
        params: tuple[Any, ...] = (now, min_gap)
        if max_checks is not None:
            sql += " AND checks < ?"
            params += (max_checks,)
        return [_to_job(row) for row in self._fetch_all(sql, params)]

    def open_count(self) -> int:
        row = self._fetch_one("SELECT COUNT(*) AS n FROM watch_jobs WHERE done = 0")
        return int(row["n"]) if row else 0

    def mark_checked(self, job_id: int, at: float) -> None:
        self._execute(
            "UPDATE watch_jobs SET last_run = ?, checks = checks + 1 WHERE id = ?",
            (at, job_id),
        )

    def mark_polled(self, job_id: int, at: float) -> None:
        self._execute("UPDATE watch_jobs SET last_run = ? WHERE id = ?", (at, job_id))

    def mark_done(self, job_id: int) -> None:
        self._execute("UPDATE watch_jobs SET done = 1 WHERE id = ?", (job_id,))

    def expired(self, now: float, max_age: float, max_checks: int | None = None) -> list[WatchJob]:
        sql = f"SELECT {_SELECT_COLUMNS} FROM watch_jobs WHERE done = 0 AND ((? - created_at) >= ?"
        params: tuple[Any, ...] = (now, max_age)
        if max_checks is not None:
            sql += " OR checks >= ?"
            params += (max_checks,)
        sql += ")"
        return [_to_job(row) for row in self._fetch_all(sql, params)]


def _dump_extra(extra: Mapping[str, Any] | None) -> str:
    if not extra:
        return ""
    return json.dumps(dict(extra), ensure_ascii=False)


def _load_extra(raw: str | None) -> dict[str, Any]:
    # A malformed value (manual edit, or written by an older version)
    # shouldn't raise and abort the whole query for every other job.
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _to_job(row: Any) -> WatchJob:
    return WatchJob(
        id=int(row["id"]),
        channel=row["channel"],
        thread_ts=row["thread_ts"],
        condition=row["condition"],
        created_at=float(row["created_at"]),
        last_run=float(row["last_run"]) if row["last_run"] is not None else None,
        msg_ts=row["msg_ts"] or "",
        checks=int(row["checks"]),
        trust=TrustLevel(int(row["trust_level"])),
        extra=_load_extra(row["extra"]),
        workdir=row["workdir"] or "",
        run_id=row["run_id"] or "",
    )
