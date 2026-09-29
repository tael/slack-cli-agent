# One `reviews` table (kind, channel, target_ts, at, result) backs
# dedup/status tracking for all three review kinds ("postmortem",
# "debug_trace", "format_review"). A `result` value that isn't valid JSON
# (hand-edited or left over from an older format) is treated as empty
# rather than raising.
#
# A row in "진행" only blocks a retry for a while. The review runs the engine
# for minutes; a restart in that window leaves a row neither completed nor
# dropped, and treating it like a completed one made re-adding the emoji do
# nothing, permanently -- while retry_hint() kept promising a retry (observed
# on rei, 2026-09-16). Blocking for a bounded window still keeps a review that
# is genuinely running from being started twice.

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass

from slack_cli_agent.storage.database import Database
from slack_cli_agent.storage.repository import SqliteRepository

#: Status values stored in `result`. Kept as constants because is_reviewed()
#: compares against them and the dashboard reads the same strings.
IN_PROGRESS = "진행"
DONE = "완료"


@dataclass(frozen=True)
class ReviewRecord:
    status: str = ""
    by: str = ""
    link: str = ""
    report: str = ""


@dataclass(frozen=True)
class StaleReview:
    """A "진행" row whose deadline passed, meaning nobody is running it now."""

    kind: str
    channel: str
    target_ts: str
    at: float
    record: ReviewRecord


class ReviewLedger(SqliteRepository):

    #: How long a "진행" row blocks a retry. Must exceed the longest a review
    #: can legitimately take -- the engine call plus the missing-split retry.
    #: Callers that know their engine's timeout should pass their own.
    DEFAULT_STALE_SEC: float = 1800.0

    def __init__(
        self,
        database: Database,
        now: Callable[[], float] | None = None,
        *,
        stale_after_sec: float | None = None,
    ) -> None:
        super().__init__(database)
        self._now = now or time.time
        self._stale_after_sec = (
            self.DEFAULT_STALE_SEC if stale_after_sec is None else stale_after_sec
        )

    def is_reviewed(self, kind: str, channel: str, target_ts: str) -> bool:
        row = self._fetch_one(
            "SELECT at, result FROM reviews WHERE kind = ? AND channel = ? AND target_ts = ?",
            (kind, channel, target_ts),
        )
        if row is None:
            return False
        if self._parse(row["result"]).status == IN_PROGRESS:
            return self._now() - float(row["at"]) < self._stale_after_sec
        return True

    def stale_in_progress(self) -> list[StaleReview]:
        deadline = self._now() - self._stale_after_sec
        rows = self._fetch_all(
            "SELECT kind, channel, target_ts, at, result FROM reviews WHERE at <= ? ORDER BY at",
            (deadline,),
        )
        found = []
        for row in rows:
            record = self._parse(row["result"])
            if record.status != IN_PROGRESS:
                continue
            found.append(
                StaleReview(
                    kind=str(row["kind"]),
                    channel=str(row["channel"]),
                    target_ts=str(row["target_ts"]),
                    at=float(row["at"]),
                    record=record,
                )
            )
        return found

    def begin(self, kind: str, channel: str, target_ts: str, *, by: str) -> None:
        self._store(kind, channel, target_ts, ReviewRecord(status=IN_PROGRESS, by=by))

    def complete(
        self,
        kind: str,
        channel: str,
        target_ts: str,
        *,
        by: str,
        link: str = "",
        report: str = "",
    ) -> None:
        self._store(
            kind,
            channel,
            target_ts,
            ReviewRecord(status=DONE, by=by, link=link, report=report),
        )

    def drop(self, kind: str, channel: str, target_ts: str) -> None:
        self._execute(
            "DELETE FROM reviews WHERE kind = ? AND channel = ? AND target_ts = ?",
            (kind, channel, target_ts),
        )

    def find(self, kind: str, channel: str, target_ts: str) -> ReviewRecord | None:
        row = self._fetch_one(
            "SELECT result FROM reviews WHERE kind = ? AND channel = ? AND target_ts = ?",
            (kind, channel, target_ts),
        )
        if row is None:
            return None
        return self._parse(row["result"])

    def _store(self, kind: str, channel: str, target_ts: str, record: ReviewRecord) -> None:
        payload = json.dumps(
            {
                "status": record.status,
                "by": record.by,
                "link": record.link,
                "report": record.report,
            },
            ensure_ascii=False,
        )
        self._execute(
            "INSERT OR REPLACE INTO reviews (kind, channel, target_ts, at, result) "
            "VALUES (?, ?, ?, ?, ?)",
            (kind, channel, target_ts, self._now(), payload),
        )

    @staticmethod
    def _parse(raw: str) -> ReviewRecord:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return ReviewRecord(status="")
        if not isinstance(data, dict):
            return ReviewRecord(status="")
        return ReviewRecord(
            status=str(data.get("status", "")),
            by=str(data.get("by", "")),
            link=str(data.get("link", "")),
            report=str(data.get("report", "")),
        )
