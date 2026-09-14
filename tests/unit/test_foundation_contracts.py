"""웨이브 1 이 기반 패키지에서 보고한 계약 구멍 3건을 고정한다.

각 보고의 출처를 테스트에 남긴다. 계약이 다시 좁아지면 여기서 실패한다.
"""

from __future__ import annotations

import sqlite3

from slack_cli_agent.config.channel import KNOWN_KEYS, ChannelConfig
from slack_cli_agent.session.ports import SessionKey, SessionRecord
from slack_cli_agent.session.store import SqliteSessionStore
from slack_cli_agent.storage.schema import MIGRATIONS, SCHEMA_VERSION


class Test신뢰등급정의는하나다:
    """W1-D 보고 — engine/base.py 가 auth 부재 때문에 TrustLevel 을 임시로 정의했다.

    정의가 두 곳이면 한쪽만 고쳤을 때 서수 비교가 조용히 어긋난다.
    """

    def test_엔진이쓰는것과권한계층이쓰는것이같은객체다(self) -> None:
        from slack_cli_agent.auth.principal import TrustLevel as 권한계층
        from slack_cli_agent.engine.base import TrustLevel as 엔진

        assert 엔진 is 권한계층

    def test_엔진모듈에별도정의가없다(self) -> None:
        import slack_cli_agent.engine.base as base

        assert "class TrustLevel" not in __import__("inspect").getsource(base)


class Test세션의실행환경이영속된다:
    """W1-E 보고 — sessions 테이블에 workdir·model 컬럼이 없었다.

    원본은 실행 환경을 화자가 아니라 대화 단위로 정하고 한 번 넓어진 값을
    좁히지 않는다. 그 규칙을 지키려면 대화 단위로 저장할 컬럼이 필요하다.
    """

    def test_마이그레이션단계로들어간다(self) -> None:
        """버전 숫자를 여기 박지 않는다.

        단계가 늘 때마다 이 테스트가 깨지는데, 그 실패는 실행 환경 컬럼과
        무관하다. 최신 버전 고정은 가장 최근 단계를 다루는 테스트 한 곳에서만
        한다.
        """
        assert 2 in [단계[0] for 단계 in MIGRATIONS]
        assert SCHEMA_VERSION >= 2

    def test_sessions테이블에실행환경컬럼이있다(self, database) -> None:
        컬럼 = {행[1] for 행 in database.connect().execute("PRAGMA table_info(sessions)")}
        assert {"workdir", "model"} <= 컬럼

    def test_v1로만든DB도최신으로올라간다(self, tmp_path) -> None:
        """이미 돌고 있는 DB 가 컬럼 추가만으로 올라가는지 본다."""
        경로 = tmp_path / "v1.db"
        연결 = sqlite3.connect(경로)
        for 문장 in MIGRATIONS[0][2]:
            연결.execute(문장)
        연결.execute("PRAGMA user_version = 1")
        연결.commit()
        연결.close()

        from slack_cli_agent.storage.database import Database

        db = Database(경로)
        db.migrate()
        컬럼 = {행[1] for 행 in db.connect().execute("PRAGMA table_info(sessions)")}
        assert {"workdir", "model"} <= 컬럼
        assert db.connect().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

    def test_실행환경이저장되고그대로돌아온다(self, database) -> None:
        저장소 = SqliteSessionStore(database)
        키 = SessionKey(scope="thread", key="C1:111.1")
        저장소.put(
            SessionRecord(
                scope=키.scope,
                key=키.key,
                session_id="s-1",
                engine="claude",
                created_at=100.0,
                last_seen_ts="111.1",
                updated_at=100.0,
                workdir="/tmp/work",
                model="opus",
            )
        )
        기록 = 저장소.get(키)
        assert 기록 is not None
        assert 기록.workdir == "/tmp/work"
        assert 기록.model == "opus"

    def test_실행환경없이저장하면빈값으로돌아온다(self, database) -> None:
        """기존 호출부가 두 인자를 안 넘겨도 깨지지 않아야 한다."""
        저장소 = SqliteSessionStore(database)
        키 = SessionKey(scope="thread", key="C1:222.2")
        저장소.put(
            SessionRecord(
                scope=키.scope,
                key=키.key,
                session_id="s-2",
                engine="claude",
                created_at=100.0,
                last_seen_ts="222.2",
                updated_at=100.0,
            )
        )
        기록 = 저장소.get(키)
        assert 기록 is not None
        assert 기록.workdir == ""
        assert 기록.model == ""


class Test채널설정키가정식필드다:
    """W1-C 보고 — 원본이 쓰는 채널 키가 KNOWN_KEYS 에 없어 extra 로 빠졌다.

    문자열 키로 조회하면 오타를 타입 검사로 검출하지 못한다. 원본 bot.py 에서
    실제로 쓰이는 키를 실측해 정식 필드로 올린다.
    """

    def test_원본이쓰는키가필드로읽힌다(self) -> None:
        설정 = ChannelConfig.from_dict(
            "C1",
            {
                "session_scope": "channel",
                "disclose_mechanism": True,
                "skills": True,
                "light_context": True,
                "rich": True,
                "chat": "quiet",
            },
        )
        assert 설정.session_scope == "channel"
        assert 설정.disclose_mechanism is True
        assert 설정.skills is True
        assert 설정.light_context is True
        assert 설정.rich is True
        assert 설정.chat == "quiet"

    def test_기본값은원본과같다(self) -> None:
        설정 = ChannelConfig.from_dict("C1", {})
        assert 설정.session_scope == "thread"
        assert 설정.disclose_mechanism is False
        assert 설정.skills is False
        assert 설정.light_context is False
        assert 설정.rich is False
        assert 설정.chat == "normal"

    def test_정식필드는extra에서빠진다(self) -> None:
        설정 = ChannelConfig.from_dict("C1", {"rich": True, "chat": "active"})
        assert 설정.extra == {}

    def test_조직고유키는extra에남는다(self) -> None:
        """org_admins 는 회사 전용이라 플러그인이 읽는다. 코어 필드가 아니다."""
        설정 = ChannelConfig.from_dict("C1", {"org_admins": ["U1"]})
        assert 설정.extra == {"org_admins": ["U1"]}

    def test_진행표시키도필드로읽힌다(self) -> None:
        """진행 표시 on/off 도 채널 설정이라 extra 가 아니라 정식 필드다.

        `disclose_mechanism` 을 extra 로 조회하다 값이 항상 꺼진 것으로 판정된
        회귀가 있었다. 같은 원인을 반복하지 않는다.
        """
        assert "progress" in KNOWN_KEYS
        assert ChannelConfig.from_dict("C1", {"progress": True}).progress is True
        assert ChannelConfig.from_dict("C1", {}).progress is False
        assert ChannelConfig.from_dict("C1", {"progress": True}).extra == {}
