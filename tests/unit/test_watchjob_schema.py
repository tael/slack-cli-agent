"""감시 작업 스키마가 원본 동작에 필요한 값을 전부 담는지 고정한다.

W2-C 보고 — 완료 시 감시 표식 리액션을 떼고 완료 표식을 붙이는 동작이 구현
불가였다. 그 리액션이 붙은 메시지의 ts 를 저장할 컬럼이 없었기 때문이다.

원본 register_watch_job 이 저장하는 항목과 대조해 정한다.
"""

from __future__ import annotations

import sqlite3

from slack_cli_agent.storage.schema import MIGRATIONS, SCHEMA_VERSION


def 컬럼목록(db) -> set[str]:
    return {행[1] for 행 in db.execute("PRAGMA table_info(watch_jobs)")}


class Test감시작업스키마:
    def test_스키마버전이올라간다(self) -> None:
        """감시 작업 컬럼이 v3 단계로 들어간 것을 고정한다. 그 뒤 단계가 더 붙어도
        이 시험이 깨지면 안 되므로 전체 버전 수가 아니라 단계 번호를 본다."""
        assert 3 in [단계[0] for 단계 in MIGRATIONS]
        assert [단계[0] for 단계 in MIGRATIONS] == list(range(1, SCHEMA_VERSION + 1))

    def test_리액션대상메시지를저장한다(self, database) -> None:
        """원본은 감시 표식을 붙인 메시지의 ts 를 들고 있다가 완료 시 바꾼다."""
        assert "msg_ts" in 컬럼목록(database.connect())

    def test_확인횟수를저장한다(self, database) -> None:
        """원본의 checks. 몇 번 확인했는지 모르면 상한을 둘 수 없다."""
        assert "checks" in 컬럼목록(database.connect())

    def test_실행권한을저장한다(self, database) -> None:
        """원본의 is_owner. 확인 작업을 어느 권한으로 실행할지가 등록 시점에 정해진다."""
        assert "trust_level" in 컬럼목록(database.connect())

    def test_플러그인이쓸자리가있다(self, database) -> None:
        """원본의 org_admin 처럼 조직 전용 값은 코어 컬럼으로 올리지 않는다."""
        assert "extra" in 컬럼목록(database.connect())

    def test_v2로만든DB도최신으로올라간다(self, tmp_path) -> None:
        경로 = tmp_path / "v2.db"
        연결 = sqlite3.connect(경로)
        for 단계 in MIGRATIONS[:2]:
            for 문장 in 단계[2]:
                연결.execute(문장)
        연결.execute("PRAGMA user_version = 2")
        연결.commit()
        연결.close()

        from slack_cli_agent.storage.database import Database

        db = Database(경로)
        db.migrate()
        assert {"msg_ts", "checks", "trust_level", "extra"} <= 컬럼목록(db.connect())
        assert db.connect().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

    def test_기존행은기본값으로남는다(self, tmp_path) -> None:
        """돌고 있는 DB 의 행이 컬럼 추가로 깨지면 안 된다."""
        경로 = tmp_path / "v2rows.db"
        연결 = sqlite3.connect(경로)
        for 단계 in MIGRATIONS[:2]:
            for 문장 in 단계[2]:
                연결.execute(문장)
        연결.execute(
            "INSERT INTO watch_jobs (channel, thread_ts, condition, created_at)"
            " VALUES ('C1', '111.1', '배포 확인', 100.0)"
        )
        연결.execute("PRAGMA user_version = 2")
        연결.commit()
        연결.close()

        from slack_cli_agent.storage.database import Database

        db = Database(경로)
        db.migrate()
        행 = db.connect().execute(
            "SELECT msg_ts, checks, trust_level, extra FROM watch_jobs"
        ).fetchone()
        assert tuple(행) == ("", 0, 0, "")


class Test실행자리컬럼:
    """v5 로 올라가는 기존 행이 빈 값으로 남고, 조회가 그 행에서 안 깨지는지 본다.

    ALTER TABLE 의 기본값만 보면 실제 행에 무엇이 들어갔는지 모른다 (코덱스 검토).
    """

    def _v4에_감시행을_넣는다(self, tmp_path):
        경로 = tmp_path / "v4rows.db"
        연결 = sqlite3.connect(경로)
        for 단계 in MIGRATIONS[:4]:
            for 문장 in 단계[2]:
                연결.execute(문장)
        연결.execute(
            "INSERT INTO watch_jobs (channel, thread_ts, condition, created_at)"
            " VALUES ('C1', '111.1', '배포 확인', 100.0)"
        )
        연결.execute("PRAGMA user_version = 4")
        연결.commit()
        연결.close()
        return 경로

    def test_기존행은_두_컬럼이_빈_문자열이다(self, tmp_path) -> None:
        from slack_cli_agent.storage.database import Database

        db = Database(self._v4에_감시행을_넣는다(tmp_path))
        db.migrate()

        행 = db.connect().execute("SELECT workdir, run_id FROM watch_jobs").fetchone()
        assert tuple(행) == ("", "")

    def test_기존행도_큐_조회로_되읽힌다(self, tmp_path) -> None:
        from slack_cli_agent.reliability.watchjobs import WatchJobQueue
        from slack_cli_agent.storage.database import Database

        db = Database(self._v4에_감시행을_넣는다(tmp_path))
        db.migrate()

        작업 = WatchJobQueue(db).due(now=200.0, min_gap=0.0)
        assert [(항목.workdir, 항목.run_id) for 항목 in 작업] == [("", "")]
