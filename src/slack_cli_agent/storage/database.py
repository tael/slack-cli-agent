"""SQLite connection and transaction handling.

Holds machine-managed state only. Prompts, personas, and channel config stay
out of the DB and in files instead, so a person can edit them without a
restart.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .schema import MIGRATIONS, SCHEMA_VERSION

log = logging.getLogger(__name__)


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._local = threading.local()

    @property
    def path(self) -> Path:
        return self._path

    def connect(self) -> sqlite3.Connection:
        """One connection per thread; sqlite3 connections aren't thread-safe."""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # isolation_level=None disables the driver's implicit transactions,
            # so we can open BEGIN IMMEDIATE ourselves and keep dequeue's
            # select-then-update atomic.
            conn = sqlite3.connect(self._path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def migrate(self) -> int:
        conn = self.connect()
        current = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if current >= SCHEMA_VERSION:
            return current

        for version, label, statements in MIGRATIONS:
            if version <= current:
                continue
            with self.transaction() as tx:
                for statement in statements:
                    tx.execute(statement)
                # PRAGMA doesn't accept parameter binding; version is a code constant.
                tx.execute(f"PRAGMA user_version={version}")
            log.info("스키마 마이그레이션 적용: v%d %s", version, label)
        return SCHEMA_VERSION

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Opens with BEGIN IMMEDIATE so dequeue's select and state change
        happen in one transaction.
        """
        conn = self.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def close(self) -> None:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
