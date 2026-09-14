# One `reviews` table (kind, channel, target_ts, at, result) backs
# dedup/status tracking for all three review kinds ("postmortem",
# "debug_trace", "format_review"). A `result` value that isn't valid JSON
# (hand-edited or left over from an older format) is treated as empty
# rather than raising.

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass

from slack_cli_agent.storage.database import Database
from slack_cli_agent.storage.repository import SqliteRepository


@dataclass(frozen=True)
class ReviewRecord:
    status: str = ""
    by: str = ""
    link: str = ""
    report: str = ""


class ReviewLedger(SqliteRepository):

    def __init__(self, database: Database, now: Callable[[], float] | None = None) -> None:
        super().__init__(database)
        self._now = now or time.time

    def is_reviewed(self, kind: str, channel: str, target_ts: str) -> bool:
        return self.find(kind, channel, target_ts) is not None

    def begin(self, kind: str, channel: str, target_ts: str, *, by: str) -> None:
        self._store(kind, channel, target_ts, ReviewRecord(status="진행", by=by))

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
            ReviewRecord(status="완료", by=by, link=link, report=report),
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
