"""세션 저장소의 SQLite 구현. 계약은 ports.SessionStore 에 있다."""

from __future__ import annotations

import sqlite3

from ..storage.database import Database
from ..storage.repository import SqliteRepository
from .ports import SessionKey, SessionRecord


class SqliteSessionStore(SqliteRepository):
    def __init__(self, db: Database) -> None:
        super().__init__(db)

    def get(self, key: SessionKey) -> SessionRecord | None:
        row = self._fetch_one(
            """
            SELECT scope, key, session_id, engine, created_at, last_seen_ts, updated_at,
                   workdir, model
              FROM sessions WHERE scope = ? AND key = ?
            """,
            (key.scope, key.key),
        )
        return self._to_record(row) if row is not None else None

    def put(self, record: SessionRecord) -> None:
        self._execute(
            """
            INSERT INTO sessions
              (scope, key, session_id, engine, created_at, last_seen_ts, updated_at,
               workdir, model)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope, key) DO UPDATE SET
              session_id = excluded.session_id,
              engine = excluded.engine,
              created_at = excluded.created_at,
              last_seen_ts = excluded.last_seen_ts,
              updated_at = excluded.updated_at,
              workdir = excluded.workdir,
              model = excluded.model
            """,
            (
                record.scope, record.key, record.session_id, record.engine,
                record.created_at, record.last_seen_ts, record.updated_at,
                record.workdir, record.model,
            ),
        )

    def touch(self, key: SessionKey, seen_ts: str) -> None:
        self._execute(
            "UPDATE sessions SET last_seen_ts = ? WHERE scope = ? AND key = ?",
            (seen_ts, key.scope, key.key),
        )

    def reassign_session_id(
        self,
        key: SessionKey,
        expected_session_id: str,
        engine: str,
        actual_session_id: str,
        now: float,
    ) -> bool:
        cursor = self._execute(
            """
            UPDATE sessions SET session_id = ?, updated_at = ?
             WHERE scope = ? AND key = ? AND session_id = ? AND engine = ?
            """,
            (actual_session_id, now, key.scope, key.key, expected_session_id, engine),
        )
        return cursor.rowcount == 1

    def expire(self, before: float) -> int:
        cursor = self._execute(
            "DELETE FROM sessions WHERE updated_at < ?", (before,)
        )
        return cursor.rowcount

    @staticmethod
    def _to_record(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            scope=row["scope"],
            key=row["key"],
            session_id=row["session_id"],
            engine=row["engine"],
            created_at=float(row["created_at"]),
            last_seen_ts=row["last_seen_ts"],
            updated_at=float(row["updated_at"]),
            workdir=row["workdir"],
            model=row["model"],
        )
