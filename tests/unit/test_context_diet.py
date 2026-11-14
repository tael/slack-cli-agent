"""S2 컨텍스트 다이어트: MCP 2단계 라우팅과 컨텍스트 임계 리셋."""

from __future__ import annotations

from dataclasses import replace

from test_pipeline import build_pipeline, make_ctx, ok_response

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.mcp_routing import (
    decode_servers,
    encode_servers,
    select_mcp_servers,
)
from slack_cli_agent.engine.base import Usage
from slack_cli_agent.session.manager import THRESHOLD_RESET_REASON, SessionManager
from slack_cli_agent.session.ports import SessionKey
from slack_cli_agent.session.store import SqliteSessionStore

KEY = SessionKey(scope="thread", key="C1:1")

ROUTING = {
    "always": ["slack", "local-rag"],
    "groups": {
        "data": {"when": ["스노우플레이크", "re:\\bsql\\b"], "servers": ["snowflake", "metabase"]},
        "docs": {"when": ["노션", "notion"], "servers": ["notion"]},
    },
    "fallback": "all",
}


class Test라우팅:
    def test_설정이_없으면_고르지_않는다(self) -> None:
        assert select_mcp_servers(None, "노션 문서") is None
        assert select_mcp_servers({}, "노션 문서") is None
        assert select_mcp_servers({"groups": "틀린 형식"}, "노션 문서") is None

    def test_맞는_묶음과_상시_묶음을_붙인다(self) -> None:
        assert select_mcp_servers(ROUTING, "노션 페이지 찾아줘") == frozenset(
            {"slack", "local-rag", "notion"}
        )

    def test_정규식과_대소문자_무시(self) -> None:
        assert select_mcp_servers(ROUTING, "이 SQL 돌려줘") == frozenset(
            {"slack", "local-rag", "snowflake", "metabase"}
        )
        assert select_mcp_servers(ROUTING, "Notion 링크") == frozenset(
            {"slack", "local-rag", "notion"}
        )

    def test_여러_묶음이_맞으면_합친다(self) -> None:
        got = select_mcp_servers(ROUTING, "스노우플레이크 결과를 노션에 정리")
        assert got == frozenset({"slack", "local-rag", "snowflake", "metabase", "notion"})

    def test_분류가_불확실하면_전부(self) -> None:
        assert select_mcp_servers(ROUTING, "안녕하세요") is None

    def test_fallback_always_는_상시_묶음만(self) -> None:
        routing = {**ROUTING, "fallback": "always"}
        assert select_mcp_servers(routing, "안녕하세요") == frozenset({"slack", "local-rag"})

    def test_재개_턴은_앞서_붙인_서버를_유지한다(self) -> None:
        got = select_mcp_servers(ROUTING, "노션 페이지", previous="snowflake,slack")
        assert got == frozenset({"slack", "local-rag", "notion", "snowflake"})

    def test_앞서_전부를_붙였으면_이번에도_전부(self) -> None:
        assert select_mcp_servers(ROUTING, "노션 페이지", previous="*") is None

    def test_잘못된_정규식은_무시한다(self) -> None:
        routing = {"groups": {"x": {"when": ["re:(", "노션"], "servers": ["notion"]}}}
        assert select_mcp_servers(routing, "노션") == frozenset({"notion"})

    def test_부호화_왕복(self) -> None:
        assert encode_servers(None) == "*"
        assert decode_servers(encode_servers(None)) is None
        assert decode_servers(encode_servers(frozenset({"b", "a"}))) == frozenset({"a", "b"})
        assert decode_servers("") == frozenset()


def _manager(store, limit: int = 150000) -> SessionManager:
    ids = iter(f"new-{i}" for i in range(1, 20))
    return SessionManager(store, RuntimeSettings(context_reset_tokens=limit),
                          now=lambda: 1000.0, new_session_id=lambda: next(ids))


class Test세션_기록과_임계:
    def test_턴_기록이_저장되고_재개_판정에_실린다(self, database) -> None:
        store = SqliteSessionStore(database)
        mgr = _manager(store)
        first = mgr.resolve(KEY, engine="claude", tier="owner")
        mgr.record_turn(KEY, first.session_id, mcp_servers="notion,slack", context_tokens=90000)
        record = store.get(KEY)
        assert record is not None
        assert record.mcp_servers == "notion,slack"
        assert record.last_context_tokens == 90000
        again = mgr.resolve(KEY, engine="claude", tier="owner")
        assert again.resume is True
        assert again.mcp_servers == "notion,slack"
        assert again.reset_reason == ""

    def test_다른_세션_기록은_건드리지_않는다(self, database) -> None:
        store = SqliteSessionStore(database)
        mgr = _manager(store)
        mgr.resolve(KEY, engine="claude", tier="owner")
        mgr.record_turn(KEY, "other", mcp_servers="x", context_tokens=1)
        record = store.get(KEY)
        assert record is not None and record.mcp_servers == "" and record.last_context_tokens == 0

    def test_임계_이상이면_새_세션(self, database) -> None:
        store = SqliteSessionStore(database)
        mgr = _manager(store)
        first = mgr.resolve(KEY, engine="claude", tier="owner")
        mgr.record_turn(KEY, first.session_id, mcp_servers="notion", context_tokens=150000)
        nxt = mgr.resolve(KEY, engine="claude", tier="owner")
        assert nxt.resume is False and nxt.rebuild_full is True
        assert nxt.session_id != first.session_id
        assert nxt.reset_reason == THRESHOLD_RESET_REASON
        assert nxt.previous_context_tokens == 150000
        # 새 세션은 서버 기록도 비운다.
        assert nxt.mcp_servers == ""
        record = store.get(KEY)
        assert record is not None and record.last_context_tokens == 0

    def test_임계_미만이면_이어받는다(self, database) -> None:
        store = SqliteSessionStore(database)
        mgr = _manager(store)
        first = mgr.resolve(KEY, engine="claude", tier="owner")
        mgr.record_turn(KEY, first.session_id, mcp_servers="", context_tokens=149999)
        assert mgr.resolve(KEY, engine="claude", tier="owner").resume is True

    def test_0_이면_끈다(self, database) -> None:
        store = SqliteSessionStore(database)
        mgr = _manager(store, limit=0)
        first = mgr.resolve(KEY, engine="claude", tier="owner")
        mgr.record_turn(KEY, first.session_id, mcp_servers="", context_tokens=10**7)
        assert mgr.resolve(KEY, engine="claude", tier="owner").resume is True

    def test_새_세션을_못_열면_이어받는다(self, database) -> None:
        store = SqliteSessionStore(database)
        calls = iter(["s-1"])

        def ids() -> str:
            return next(calls)  # 두 번째 호출에서 StopIteration

        mgr = SessionManager(store, RuntimeSettings(context_reset_tokens=10),
                             now=lambda: 1000.0, new_session_id=ids)
        first = mgr.resolve(KEY, engine="claude", tier="owner")
        mgr.record_turn(KEY, first.session_id, mcp_servers="", context_tokens=100)
        again = mgr.resolve(KEY, engine="claude", tier="owner")
        assert again.resume is True and again.session_id == "s-1"


def _big(session_id: str = "sess-1"):
    return replace(
        ok_response(session_id=session_id),
        turns=2,
        usage=Usage(input_tokens=4, output_tokens=10,
                    cache_creation_tokens=100000, cache_read_tokens=200000),
    )


class Test파이프라인:
    def test_라우팅_선택이_요청에_실리고_세션에_남는다(self) -> None:
        pipeline, parts = build_pipeline(responses=[ok_response(), ok_response()])
        pipeline._mcp_routing = ROUTING
        pipeline.handle(make_ctx(user="UOWNER", ts="1.1", thread_ts="1.1", text="노션 찾아줘"))
        pipeline.handle(make_ctx(user="UOWNER", ts="1.2", thread_ts="1.1", text="SQL 돌려줘"))
        first, second = parts["runner"].calls
        assert first.mcp_selection == frozenset({"slack", "local-rag", "notion"})
        assert second.resume is True
        # 앞 턴의 notion 이 재개 턴에도 남는다.
        assert second.mcp_selection == frozenset(
            {"slack", "local-rag", "notion", "snowflake", "metabase"}
        )

    def test_라우팅이_없으면_선택하지_않는다(self) -> None:
        pipeline, parts = build_pipeline(responses=[ok_response()])
        pipeline.handle(make_ctx(user="UOWNER", ts="1.1", thread_ts="1.1", text="노션"))
        assert parts["runner"].calls[0].mcp_selection is None

    def test_컨텍스트가_크면_다음_요청은_새_세션(self) -> None:
        ids = iter(f"session-{i}" for i in range(1, 10))
        store_mgr = SessionManager(
            __import__("test_pipeline").InMemorySessionStore(),
            RuntimeSettings(context_reset_tokens=150000),
            now=lambda: 1000.0, new_session_id=lambda: next(ids),
        )
        pipeline, parts = build_pipeline(
            responses=[_big("session-1"), ok_response(session_id="session-2")],
            sessions=store_mgr,
        )
        pipeline.handle(make_ctx(user="UOWNER", ts="1.1", thread_ts="1.1"))
        pipeline.handle(make_ctx(user="UOWNER", ts="1.2", thread_ts="1.1"))
        first, second = parts["runner"].calls
        assert second.resume is False
        assert second.session_id != first.session_id
        assert "새 세션으로 이어 갑니다" in second.prompt
        record = parts["audit"].records[-1]
        assert record["context_reset"] is True
        assert record["context_reset_reason"] == "threshold"
        # 첫 턴 기록에는 리셋이 없다.
        assert parts["audit"].records[0]["context_reset"] is False
        assert "context_reset_reason" not in parts["audit"].records[0]
