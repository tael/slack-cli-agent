# Audit records go to both the DB (`audit` table) and an `audit.jsonl` file.
# External tools and dashboards read the jsonl, so field names there must
# stay stable; the DB is for querying and aggregation.

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from enum import Enum
from pathlib import Path
from typing import Any

from ..core.jsonsafe import dump_json
from ..storage.database import Database
from ..storage.repository import SqliteRepository


class IncidentKind(str, Enum):
    """Every kind string written to the `audit` table's `kind` column.

    Single source for these strings — pipeline.py, publisher.py, and
    metrics.py all read from here instead of repeating the literal, so a
    rename can't leave one caller writing a kind nothing reads back.
    """

    REQUEST = "request"
    SPLIT = "split"
    SPLIT_BROKEN = "split_broken"
    BLOCKS_REJECTED = "blocks_rejected"
    POST_FAILED = "post_failed"
    LATE_ADDENDUM = "late_addendum"
    WRONG_ADDRESSEE = "wrong_addressee"
    REWRITE_LOSS = "rewrite_loss"
    SILENT = "silent"

    def __str__(self) -> str:
        return self.value


REQUEST_KIND = IncidentKind.REQUEST.value

# Kinds that count as an "incident" for the reliability/quality rollups —
# REQUEST is excluded since it's the baseline traffic, not an incident.
INCIDENT_KINDS: tuple[IncidentKind, ...] = tuple(k for k in IncidentKind if k is not IncidentKind.REQUEST)

# Every row `record()` writes always carries a kind (see `line` below), so
# this only guards a hand-edited or otherwise corrupted row.
_DEFAULT_KIND_FOR_MISSING = IncidentKind.REQUEST.value


def normalize_kind(kind: str | None) -> str:
    return kind if kind else _DEFAULT_KIND_FOR_MISSING


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
            (at, str(kind), channel, thread_ts, dump_json(fields)),
        )
        line = {"kind": str(kind), "channel": channel, "thread_ts": thread_ts, "at": at}
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
        user: str = "",
        # None means the engine didn't report a turn count (e.g. Codex) —
        # distinct from 0 turns, which is a real value.
        turns: int | None = None,
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
            user=user,
            turns=turns,
            **extra,
        )

    def _append_jsonl(self, line: Mapping[str, Any]) -> None:
        self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._jsonl_path, "a", encoding="utf-8") as f:
            f.write(dump_json(line) + "\n")



#: Depth at which nesting is replaced by a placeholder. Well past anything an
#: audit field actually carries, so reaching it means the value is malformed.
