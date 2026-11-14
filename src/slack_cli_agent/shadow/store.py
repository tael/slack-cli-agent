"""Shadow evaluation state in state.db (G4)."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from ..storage.database import Database
from ..storage.repository import SqliteRepository


class ShadowStore(SqliteRepository):
    def __init__(self, database: Database, now: Callable[[], float] = time.time) -> None:
        super().__init__(database)
        self._now = now

    def seen(self, source: str) -> str | None:
        row = self._fetch_one("SELECT last_ts FROM shadow_seen WHERE source = ?", (source,))
        return str(row["last_ts"]) if row else None

    def mark_seen(self, source: str, ts: str) -> None:
        """Only moves forward: a backfill that finishes after a newer poll must
        not rewind the position and repost what came after it."""
        prev = self.seen(source)
        if prev is not None and float(prev) >= float(ts):
            return
        self._execute(
            "INSERT INTO shadow_seen (source, last_ts) VALUES (?, ?) "
            "ON CONFLICT(source) DO UPDATE SET last_ts = excluded.last_ts",
            (source, ts),
        )

    def item(self, source: str, ts: str) -> dict[str, Any]:
        row = self._fetch_one(
            "SELECT payload FROM shadow_items WHERE source = ? AND ts = ?", (source, ts)
        )
        return self._parse(row["payload"]) if row else {}

    def save(self, item: dict[str, Any]) -> None:
        self._execute(
            "INSERT INTO shadow_items (source, ts, payload, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(source, ts) DO UPDATE SET payload = excluded.payload, "
            "updated_at = excluded.updated_at",
            (item["source"], item["ts"], json.dumps(item, ensure_ascii=False), self._now()),
        )

    def update(self, source: str, ts: str, **fields: Any) -> dict[str, Any]:
        item = dict(self.item(source, ts), source=source, ts=ts, **fields)
        self.save(item)
        return item

    def items(self) -> list[dict[str, Any]]:
        rows = self._fetch_all("SELECT payload FROM shadow_items ORDER BY ts")
        return [self._parse(r["payload"]) for r in rows]

    @staticmethod
    def _parse(payload: str) -> dict[str, Any]:
        try:
            data = json.loads(payload)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
