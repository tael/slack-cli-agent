"""Shared repository base: single place for connection access and transactions."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from .database import Database


class SqliteRepository:
    """Base for SQLite-backed repositories. Shared implementation detail, not
    a contract — each domain package's Protocol defines that.
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
