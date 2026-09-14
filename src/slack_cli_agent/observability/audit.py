# Audit records go to both the DB (`audit` table) and an `audit.jsonl` file.
# External tools and dashboards read the jsonl, so field names there must
# stay stable; the DB is for querying and aggregation.

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..storage.database import Database
from ..storage.repository import SqliteRepository

REQUEST_KIND = "request"


class AuditLog(SqliteRepository):
    def __init__(
        self,
        db: Database,
        jsonl_path: Path,
        now: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(db)
        self._jsonl_path = jsonl_path
        self._now = now

    def record(
        self,
        kind: str,
        *,
        channel: str = "",
        thread_ts: str = "",
        **fields: Any,
    ) -> None:
        at = self._now()
        self._execute(
            "INSERT INTO audit (at, kind, channel, thread_ts, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (at, kind, channel, thread_ts, json.dumps(fields, ensure_ascii=False)),
        )
        line = {"kind": kind, "channel": channel, "thread_ts": thread_ts, "at": at}
        line.update(fields)
        self._append_jsonl(line)

    def record_request(
        self,
        *,
        channel: str,
        thread_ts: str,
        message_ts: str,
        session_id: str,
        resumed: bool,
        model: str,
        effort: str,
        elapsed: float,
        ok: bool,
        first_reaction_sec: float | None = None,
        queue_wait_sec: float | None = None,
        usage: Mapping[str, Any] | None = None,
        **extra: Any,
    ) -> None:
        self.record(
            REQUEST_KIND,
            channel=channel,
            thread_ts=thread_ts,
            message_ts=message_ts,
            session_id=session_id,
            resumed=resumed,
            model=model,
            effort=effort,
            elapsed=elapsed,
            ok=ok,
            first_reaction_sec=first_reaction_sec,
            queue_wait_sec=queue_wait_sec,
            usage=dict(usage) if usage is not None else None,
            **extra,
        )

    def _append_jsonl(self, line: Mapping[str, Any]) -> None:
        self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
