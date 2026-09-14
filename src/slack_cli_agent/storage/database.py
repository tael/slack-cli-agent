"""SQLite 커넥션과 트랜잭션.

기계가 쓰는 상태를 한 파일에 모은다. 프롬프트·페르소나·채널 설정은 여기 넣지
않는다 — 파일이어야 사람이 편집하고 재기동 없이 반영된다.
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
        """스레드마다 커넥션 하나. sqlite3 커넥션은 스레드 간 공유가 안 된다."""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # isolation_level=None 으로 두어 드라이버의 암묵적 트랜잭션을 끈다.
            # BEGIN IMMEDIATE 를 직접 열어야 디큐의 조회와 전이가 한 트랜잭션이 된다
            conn = sqlite3.connect(self._path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def migrate(self) -> int:
        """적용된 마지막 버전을 돌려준다. 이미 최신이면 아무것도 실행하지 않는다."""
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
                # PRAGMA 는 파라미터 바인딩을 받지 않는다. 값은 코드 상수뿐이다
                tx.execute(f"PRAGMA user_version={version}")
            log.info("스키마 마이그레이션 적용: v%d %s", version, label)
        return SCHEMA_VERSION

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """IMMEDIATE 로 연다. 디큐가 조회와 상태 전이를 한 트랜잭션에서 한다."""
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
