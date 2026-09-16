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
    def test_코드보다_새_스키마면_경고를_남긴다(self, tmp_path: Path, caplog) -> None:
        """editable 설치에서 한쪽 프로세스만 재기동하면 옛 코드가 새 스키마
        위에서 돈다. 막지는 않되 로그에는 남겨야 그 상태를 확인할 수 있다
        (sca-4cg)."""
        path = tmp_path / "state.db"
        db = Database(path)
        db.migrate()
        db.connect().execute(f"PRAGMA user_version={SCHEMA_VERSION + 3}")
        with caplog.at_level("WARNING"):
            assert Database(path).migrate() == SCHEMA_VERSION + 3
        assert [r for r in caplog.records if str(SCHEMA_VERSION + 3) in r.getMessage()]

    def test_같은_판이면_경고하지_않는다(self, tmp_path: Path, caplog) -> None:
        db = Database(tmp_path / "state.db")
        db.migrate()
        with caplog.at_level("WARNING"):
            db.migrate()
        assert not caplog.records

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


class FakeConn:
    """journal_mode 만 흉내내는 대역. 실제 잠금 충돌은 다른 프로세스가
    있어야 나므로 단위 시험에서는 예외로 대신한다."""

    def __init__(self, mode: str, fail_times: int = 0) -> None:
        self.mode = mode
        self.fail_times = fail_times
        self.executed: list[str] = []

    def execute(self, sql: str):  # type: ignore[no-untyped-def]
        self.executed.append(sql)
        if sql == "PRAGMA journal_mode":
            return FakeCursor((self.mode,))
        if sql == "PRAGMA journal_mode=WAL":
            if self.fail_times > 0:
                self.fail_times -= 1
                raise sqlite3.OperationalError("database is locked")
            self.mode = "wal"
            return FakeCursor(("wal",))
        return FakeCursor(None)


class FakeCursor:
    def __init__(self, row) -> None:  # type: ignore[no-untyped-def]
        self._row = row

    def fetchone(self):  # type: ignore[no-untyped-def]
        return self._row


class TestWAL전환:
    """sca-fly — 첫 기동에서 ingress 와 worker 가 같은 새 DB 를 동시에 열다
    PRAGMA journal_mode=WAL 이 잠금으로 죽었다."""

    def test_이미_WAL_이면_다시_설정하지_않는다(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "state.db")
        conn = FakeConn(mode="wal")
        db._enable_wal(conn)  # type: ignore[arg-type]
        assert "PRAGMA journal_mode=WAL" not in conn.executed

    def test_잠금이면_다시_시도한다(self, tmp_path: Path) -> None:
        잔_시간: list[float] = []
        db = Database(tmp_path / "state.db", sleep=잔_시간.append)
        conn = FakeConn(mode="delete", fail_times=2)
        db._enable_wal(conn)  # type: ignore[arg-type]
        assert conn.mode == "wal"
        assert len(잔_시간) == 2

    def test_끝까지_잠겨_있으면_예외를_낸다(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "state.db", sleep=lambda _: None)
        conn = FakeConn(mode="delete", fail_times=99)
        with pytest.raises(sqlite3.OperationalError):
            db._enable_wal(conn)  # type: ignore[arg-type]


class TestTransaction:
    def test_예외가_나면_되돌린다(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "state.db")
        db.migrate()

        with pytest.raises(RuntimeError), db.transaction() as conn:
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
