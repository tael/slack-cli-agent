"""기계 상태 DB. 커넥션, 트랜잭션, 마이그레이션."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from slack_cli_agent.storage.database import Database
from slack_cli_agent.storage.repository import SqliteRepository
from slack_cli_agent.storage.schema import SCHEMA_VERSION


def table_names(db: Database) -> set[str]:
    rows = db.connect().execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {row["name"] for row in rows}


class TestMigration:
    def test_새_DB_는_최신_버전으로_생성된다(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "state.db")
        assert db.migrate() == SCHEMA_VERSION
        assert {"jobs", "sessions", "audit", "reviews", "watch_jobs"} <= table_names(db)

    def test_두_번_적용해도_실패하지_않는다(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "state.db")
        db.migrate()
        assert db.migrate() == SCHEMA_VERSION

    def test_적용된_버전이_파일에_남는다(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        Database(path).migrate()
        version = Database(path).connect().execute("PRAGMA user_version").fetchone()[0]
        assert version == SCHEMA_VERSION

    def test_상위_디렉터리가_없어도_생성한다(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "없는디렉터리" / "state.db")
        db.migrate()
        assert db.path.exists()


class TestConnection:
    def test_WAL_모드로_연다(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "state.db")
        mode = db.connect().execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_스레드마다_커넥션이_다르다(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "state.db")
        db.migrate()
        seen: list[int] = []

        def record() -> None:
            seen.append(id(db.connect()))

        worker = threading.Thread(target=record)
        worker.start()
        worker.join()

        assert seen and seen[0] != id(db.connect())


class TestTransaction:
    def test_예외가_나면_되돌린다(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "state.db")
        db.migrate()

        with pytest.raises(RuntimeError):
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO audit (at, kind, payload) VALUES (1, 'k', '{}')"
                )
                raise RuntimeError("중단")

        assert db.connect().execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 0

    def test_정상_종료하면_남는다(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "state.db")
        db.migrate()

        with db.transaction() as conn:
            conn.execute("INSERT INTO audit (at, kind, payload) VALUES (1, 'k', '{}')")

        assert db.connect().execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 1


class TestSqliteRepository:
    def test_행_조회를_공통으로_제공한다(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "state.db")
        db.migrate()

        class AuditRepo(SqliteRepository):
            def add(self, kind: str) -> None:
                self._execute(
                    "INSERT INTO audit (at, kind, payload) VALUES (?, ?, '{}')",
                    (1.0, kind),
                )

            def kinds(self) -> list[str]:
                return [r["kind"] for r in self._fetch_all("SELECT kind FROM audit")]

        repo = AuditRepo(db)
        repo.add("배포")
        assert repo.kinds() == ["배포"]

    def test_트랜잭션을_상속받은_저장소가_쓸_수_있다(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "state.db")
        db.migrate()

        class Repo(SqliteRepository):
            def insert_twice_and_fail(self) -> None:
                with self._transaction() as conn:
                    conn.execute(
                        "INSERT INTO audit (at, kind, payload) VALUES (1, 'a', '{}')"
                    )
                    raise RuntimeError("중단")

        with pytest.raises(RuntimeError):
            Repo(db).insert_twice_and_fail()
        assert db.connect().execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 0
