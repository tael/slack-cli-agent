"""SQLite connection and transaction handling.

Holds machine-managed state only. Prompts, personas, and channel config stay
out of the DB and in files instead, so a person can edit them without a
restart.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from .schema import MIGRATIONS, SCHEMA_VERSION

log = logging.getLogger(__name__)


class Database:
    #: PRAGMA journal_mode=WAL rewrites the file header and needs the
    #: database to itself. Two processes opening the same brand-new file at
    #: once collide there, and rei's first boot spent the whole busy timeout
    #: waiting before ingress died (sca-fly). Each failed attempt can spend
    #: that whole timeout, so the retry count stays small -- boot must not
    #: hang for minutes on a database nobody is going to release.
    WAL_ATTEMPTS: int = 3
    WAL_RETRY_SEC: float = 0.5

    #: Lock wait for everything that is not on a latency-sensitive path. Long
    #: on purpose -- a boot that collides with another process should wait
    #: rather than fail.
    DEFAULT_BUSY_TIMEOUT_MS: int = 30_000

    def __init__(self, path: Path, *, sleep: Callable[[float], None] = time.sleep) -> None:
        self._path = path
        self._local = threading.local()
        self._sleep = sleep

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
            # The budget has to be read here, not by a caller that already has a
            # connection: this thread's first DB statement is _enable_wal below,
            # and it must already be under the budget (sca-9l1). timeout= and
            # busy_timeout set the same lock wait; both are set from one value so
            # they can't drift.
            wait_ms = self._busy_timeout_ms()
            conn = sqlite3.connect(self._path, timeout=wait_ms / 1000, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={wait_ms}")
            self._enable_wal(conn)
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def _busy_timeout_ms(self) -> int:
        budget: int | None = getattr(self._local, "budget_ms", None)
        return budget if budget is not None else self.DEFAULT_BUSY_TIMEOUT_MS

    @contextmanager
    def latency_budget(self, seconds: float) -> Iterator[None]:
        """Caps how long DB calls on this thread wait on a lock.

        For the socket handler threads: they ack the Slack event before the
        handler runs, so a long lock wait does not delay that event's ack, but
        it holds one of the ten pool slots and delays every event behind it
        (sca-9l1). Everything else -- migrations, the WAL switch, the worker --
        keeps the long wait.

        Thread-local, like the connection it applies to.
        """
        previous: int | None = getattr(self._local, "budget_ms", None)
        self._local.budget_ms = max(1, int(seconds * 1000))
        try:
            self._apply_busy_timeout()
            yield
        finally:
            self._local.budget_ms = previous
            self._apply_busy_timeout()

    def _apply_busy_timeout(self) -> None:
        """Only touches a connection this thread already made. A connection
        made later reads the budget in connect()."""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms()}")

    def _enable_wal(self, conn: sqlite3.Connection) -> None:
        """Skips the change when the file is already WAL -- that is every
        boot after the first, and it is the only case the busy timeout
        cannot help with."""
        if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
            return
        # Under a latency budget the retry sleeps alone would outlast it, and
        # the caller is a socket handler thread holding a pool slot (sca-9l1).
        attempts = 1 if getattr(self._local, "budget_ms", None) is not None else self.WAL_ATTEMPTS
        for attempt in range(1, attempts + 1):
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as exc:
                if attempt == attempts:
                    raise
                log.warning(
                    "WAL 전환이 잠금으로 실패했다. 같은 DB 를 여는 다른 프로세스가 있다 : %s", exc
                )
                self._sleep(self.WAL_RETRY_SEC * attempt)

    def migrate(self) -> int:
        conn = self.connect()
        current = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if current > SCHEMA_VERSION:
            # editable 설치에서 한쪽 프로세스만 재기동하면 그 프로세스가 스키마를
            # 올리고 다른 쪽은 옛 코드로 계속 돈다. 마이그레이션은 덧붙이는
            # 형태라 대개 호환되므로 막지 않고 알리기만 한다 (sca-4cg).
            log.warning(
                "DB 스키마가 이 코드보다 새롭다. DB v%d, 코드 v%d", current, SCHEMA_VERSION
            )
            return current
        if current == SCHEMA_VERSION:
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
