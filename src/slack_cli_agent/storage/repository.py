"""저장소 공통 기반. 커넥션 접근과 트랜잭션 진입을 한 번만 쓴다."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from .database import Database


class SqliteRepository:
    """SQLite 를 쓰는 저장소의 기반.

    이 클래스는 구현끼리 공유하는 것이고 계약이 아니다. 계약은 각 도메인
    패키지의 Protocol 이 정의한다.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    @property
    def database(self) -> Database:
        return self._db

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self._db.connect().execute(sql, params)

    def _fetch_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._execute(sql, params).fetchone()
        return row

    def _fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self._execute(sql, params).fetchall()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._db.transaction() as conn:
            yield conn
