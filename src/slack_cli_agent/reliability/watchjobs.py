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
from ..guard.watch import distinct_watch_targets, same_watch_target
from ..storage.database import Database
from ..storage.repository import SqliteRepository

log = logging.getLogger(__name__)

_SELECT_COLUMNS = (
    "id, channel, thread_ts, condition, created_at, last_run, "
    "msg_ts, checks, trust_level, extra, workdir, run_id, due_at"
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
    due_at: float | None = None
    """Epoch deadline from a `[[WATCH@YYYY-MM-DD: ...]]` tag. None means no
    deadline — checked on the usual cadence, like every row written before
    this existed."""


@dataclass(frozen=True)
class PendingReport:
    """A completion report whose post failed and still owes delivery."""

    job_id: int
    channel: str
    thread_ts: str
    body: str


@runtime_checkable
class ActiveWatchPort(Protocol):
    """What the worker needs. WatchJobPort satisfies it."""

    def active_watch(self, channel: str, msg_ts: str) -> bool: ...


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
        due_at: float | None = None,
    ) -> int:
        """Registers a watch job and returns its row id."""

    def due(self, now: float, min_gap: float, max_checks: int | None = None) -> list[WatchJob]:
        """Jobs due for a check. Excludes jobs checked within `min_gap` (each
        check re-invokes the engine, so this throttles frequency), jobs at
        `max_checks` (those must be picked up by `expired` with the same
        limit, or they'd be stuck forever), and jobs with a `due_at` deadline
        that hasn't arrived yet."""

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

    def active_watch(self, channel: str, msg_ts: str) -> bool:
        """Whether that message is being watched right now. reclaim reads this
        to pick the mark (sca-o1e)."""

    def hold_report(self, job_id: int, body: str) -> None:
        """Keeps a completion report whose post failed, for a later retry."""

    def release_report(self, job_id: int) -> None:
        """Drops a held report once it has actually been posted."""

    def pending_reports(self) -> list[PendingReport]:
        """Reports still owed, oldest first."""

    def open_siblings(self, job: WatchJob) -> list[int]:
        """Open watches in the same thread registered after `job`."""


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
        due_at: float | None = None,
    ) -> int:
        self._supersede_same_target(channel, thread_ts, condition, due_at)
        if not run_id:
            condition = self._fold_open_siblings(channel, thread_ts, condition, msg_ts, due_at)
        cursor = self._execute(
            "INSERT INTO watch_jobs (channel, thread_ts, condition, created_at, "
            "last_run, done, msg_ts, checks, trust_level, extra, workdir, run_id, due_at) "
            "VALUES (?, ?, ?, ?, NULL, 0, ?, 0, ?, ?, ?, ?, ?) "
            "ON CONFLICT DO NOTHING",
            (
                channel, thread_ts, condition, self._now(),
                msg_ts, int(trust), _dump_extra(extra), workdir, run_id, due_at,
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

    def _supersede_same_target(
        self, channel: str, thread_ts: str, condition: str, due_at: float | None,
    ) -> None:
        """Closes an existing active watch in this thread that names the same
        target, before a new one is inserted. Matches bot.py:10834-10845's
        replace-on-update behavior in `register_watch_job`: the conversation
        is updating one promise, not accumulating a new one on top of it. A
        deadlined watch only replaces another deadlined one, and an
        undeadlined watch only replaces another undeadlined one -- mixing the
        two would silently drop a commitment the other kind made.

        A no-op for descriptions with no long identifying token (plain
        Korean sentences, mostly) -- `same_watch_target` always returns False
        for those, so ordinary watches never collide here.
        """
        candidates = self._fetch_all(
            "SELECT id, condition, due_at FROM watch_jobs "
            "WHERE done = 0 AND channel = ? AND thread_ts = ?",
            (channel, thread_ts),
        )
        has_due = due_at is not None
        stale = [
            row["id"] for row in candidates
            if bool(row["due_at"]) == has_due and same_watch_target(row["condition"], condition)
        ]
        if not stale:
            return
        placeholders = ", ".join("?" for _ in stale)
        self._execute(
            f"UPDATE watch_jobs SET done = 1 WHERE id IN ({placeholders})", tuple(stale),
        )
        log.info("감시 교체 : 같은 스레드 기존 %d건 제거", len(stale))

    def _fold_open_siblings(
        self, channel: str, thread_ts: str, condition: str, msg_ts: str, due_at: float | None,
    ) -> str:
        """Merges still-open condition watches of this thread into the new one.

        2026-11-06 test-agent: a restart was watched twice in one thread.
        Job 25 checked the pid file, the request file and the account value;
        job 26, added two minutes later after old workers were seen alive,
        checked that those workers were gone. `same_watch_target` compares
        long identifier tokens only, so the two never matched. Job 25 then
        finished on its own and posted "restart confirmed" while job 26 was
        still open, and the thread read the restart as done.

        A new condition watch in a thread that already has one is the
        conversation tightening what "done" means, not a second unrelated
        promise. The old conditions are carried into the new row and the old
        row is closed, so only one report can go out and it must satisfy
        every condition. Result-file watches (`run_id` set) keep their own
        rows: their completion is read from an exit status, not from text.
        The same deadline-class rule as `_supersede_same_target` applies.
        Watches that clearly name another target (two different dag runs)
        stay separate, and a rerun of the same message is left to the
        unique-index path that keeps enqueue idempotent (sca-efe).
        """
        rows = self._fetch_all(
            "SELECT id, condition, due_at, msg_ts FROM watch_jobs "
            "WHERE done = 0 AND channel = ? AND thread_ts = ? AND COALESCE(run_id, '') = '' "
            "ORDER BY id",
            (channel, thread_ts),
        )
        has_due = due_at is not None
        older = [
            row for row in rows
            if bool(row["due_at"]) == has_due
            and not (msg_ts and row["msg_ts"] == msg_ts)
            and not distinct_watch_targets(row["condition"], condition)
        ]
        if not older:
            return condition
        ids = tuple(int(row["id"]) for row in older)
        placeholders = ", ".join("?" for _ in ids)
        self._execute(f"UPDATE watch_jobs SET done = 1 WHERE id IN ({placeholders})", ids)
        log.info("감시 병합 : 같은 스레드 조건 감시 %d건을 새 감시에 합침", len(ids))
        earlier = "\n".join(f"- {row['condition']}" for row in older)
        return (
            f"{condition}\n\n"
            "앞서 같은 스레드에 등록된 아래 조건도 모두 충족해야 완료다.\n"
            f"{earlier}"
        )

    def open_siblings(self, job: WatchJob) -> list[int]:
        """Ids of newer open condition watches in this thread about the same work.

        A newer open watch means the conversation asked for more than `job`
        checks, so `job` finishing is not the whole thing finishing. Watches
        that clearly name another target, result-file watches, and the other
        deadline class are excluded -- those are separate promises with their
        own reports.
        """
        rows = self._fetch_all(
            "SELECT id, condition, due_at FROM watch_jobs WHERE done = 0 AND channel = ? "
            "AND thread_ts = ? AND id > ? AND COALESCE(run_id, '') = '' ORDER BY id",
            (job.channel, job.thread_ts, job.id),
        )
        has_due = job.due_at is not None
        return [
            int(row["id"]) for row in rows
            if bool(row["due_at"]) == has_due
            and not distinct_watch_targets(row["condition"], job.condition)
        ]

    def active_watch(self, channel: str, msg_ts: str) -> bool:
        if not msg_ts:
            return False
        row = self._fetch_one(
            "SELECT 1 FROM watch_jobs WHERE done = 0 AND channel = ? AND msg_ts = ?",
            (channel, msg_ts),
        )
        return row is not None

    def due(self, now: float, min_gap: float, max_checks: int | None = None) -> list[WatchJob]:
        sql = (
            f"SELECT {_SELECT_COLUMNS} FROM watch_jobs WHERE done = 0 "
            "AND (? - COALESCE(last_run, created_at)) >= ? "
            # A deadline not yet reached means don't look at all — not "look
            # less often". A week-long promise would otherwise still wake the
            # engine every min_gap seconds for nothing (bot.py:10936-10939).
            "AND (due_at IS NULL OR due_at <= ?)"
        )
        params: tuple[Any, ...] = (now, min_gap, now)
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

    def hold_report(self, job_id: int, body: str) -> None:
        self._execute(
            "UPDATE watch_jobs SET pending_report = ? WHERE id = ?", (body, job_id)
        )

    def release_report(self, job_id: int) -> None:
        self._execute(
            "UPDATE watch_jobs SET pending_report = '' WHERE id = ?", (job_id,)
        )

    def pending_reports(self) -> list[PendingReport]:
        rows = self._fetch_all(
            "SELECT id, channel, thread_ts, pending_report FROM watch_jobs "
            "WHERE pending_report != '' ORDER BY id"
        )
        return [
            PendingReport(
                job_id=int(row["id"]),
                channel=str(row["channel"]),
                thread_ts=str(row["thread_ts"]),
                body=str(row["pending_report"]),
            )
            for row in rows
        ]

    def expired(self, now: float, max_age: float, max_checks: int | None = None) -> list[WatchJob]:
        # The age clock starts at the deadline, not at registration, when one
        # is set — the waiting period before a deadline must not eat into the
        # time budget for giving up (bot.py:11028-11030).
        sql = (
            f"SELECT {_SELECT_COLUMNS} FROM watch_jobs WHERE done = 0 "
            "AND ((? - COALESCE(due_at, created_at)) >= ?"
        )
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
        due_at=float(row["due_at"]) if row["due_at"] is not None else None,
    )
