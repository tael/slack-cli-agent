"""화자 등급이 다른 턴은 세션을 이어받지 않는다.

claude 2.1.319 는 --resume 턴의 --append-system-prompt 를 반영하지 않고 세션을
연 턴의 시스템 지침을 그대로 보낸다 (2026-11-07 운영 세션 4efce5d9 의 위임 턴
요청이 첫 소유자 턴과 글자까지 같았다, 같은 날 CLI 시험에서도 재현). codex 도
첫 턴의 지침이 세션 맥락에 남는다. 그래서 소유자 턴이 연 세션을 위임, 봇,
비소유자 턴이 이어받으면 소유자 지침이 남는다. 등급이 바뀌면 새 세션을 연다.
같은 등급이 이어받는 기존 동작(C15)은 그대로다.
"""

from __future__ import annotations

from test_pipeline import build_pipeline, make_ctx, ok_response

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.session.manager import SessionManager
from slack_cli_agent.session.ports import SessionKey, SessionRecord
from slack_cli_agent.session.store import SqliteSessionStore

KEY = SessionKey(scope="thread", key="C1:1")


def _manager(store) -> SessionManager:
    ids = iter(f"new-{i}" for i in range(1, 10))
    return SessionManager(store, RuntimeSettings(), now=lambda: 1000.0,
                          new_session_id=lambda: next(ids))


class Test판정:
    def test_같은_등급은_이어받는다(self, database) -> None:
        mgr = _manager(SqliteSessionStore(database))
        first = mgr.resolve(KEY, engine="claude", tier="owner")
        again = mgr.resolve(KEY, engine="claude", tier="owner")
        assert again.resume is True
        assert again.session_id == first.session_id

    def test_등급이_바뀌면_새_세션을_연다(self, database) -> None:
        mgr = _manager(SqliteSessionStore(database))
        first = mgr.resolve(KEY, engine="claude", tier="owner")
        other = mgr.resolve(KEY, engine="claude", tier="delegated")
        assert other.resume is False
        assert other.rebuild_full is True
        assert other.session_id != first.session_id

    def test_새_세션은_연_턴의_등급을_기록한다(self, database) -> None:
        store = SqliteSessionStore(database)
        mgr = _manager(store)
        mgr.resolve(KEY, engine="claude", tier="owner")
        mgr.resolve(KEY, engine="claude", tier="delegated")
        record = store.get(KEY)
        assert record is not None and record.tier == "delegated"
        # 이어받은 위임 턴 다음의 소유자 턴도 다시 새 세션이다.
        assert mgr.resolve(KEY, engine="claude", tier="owner").resume is False

    def test_등급이_없는_예전_기록은_이어받지_않는다(self, database) -> None:
        store = SqliteSessionStore(database)
        store.put(SessionRecord(scope=KEY.scope, key=KEY.key, session_id="old", engine="claude",
                                created_at=999.0, last_seen_ts="", updated_at=999.0))
        decision = _manager(store).resolve(KEY, engine="claude", tier="read")
        assert decision.resume is False

    def test_재시작용_reset도_등급을_기록한다(self, database) -> None:
        store = SqliteSessionStore(database)
        mgr = _manager(store)
        mgr.reset(KEY, engine="claude", tier="bot")
        assert mgr.resolve(KEY, engine="claude", tier="bot").resume is True


class Test파이프라인:
    def test_소유자_세션을_비소유자_턴이_이어받지_않는다(self) -> None:
        pipeline, parts = build_pipeline(responses=[ok_response(), ok_response()])
        pipeline.handle(make_ctx(user="UOWNER", ts="1.1", thread_ts="1.1"))
        pipeline.handle(make_ctx(user="U1", ts="1.2", thread_ts="1.1"))
        first, second = parts["runner"].calls
        assert first.resume is False
        assert second.resume is False
        assert second.session_id != "sess-1"

    def test_같은_화자_등급은_세션을_이어받는다(self) -> None:
        pipeline, parts = build_pipeline(responses=[ok_response(), ok_response()])
        pipeline.handle(make_ctx(user="UOWNER", ts="1.1", thread_ts="1.1"))
        pipeline.handle(make_ctx(user="UOWNER", ts="1.2", thread_ts="1.1"))
        second = parts["runner"].calls[1]
        assert second.resume is True
        assert second.session_id == "sess-1"
