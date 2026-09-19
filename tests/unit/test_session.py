"""세션 관리 계약과 정책 시험. SessionStore 는 영속화, SessionManager 는 판정."""

from __future__ import annotations

import pytest

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.session.manager import SessionManager
from slack_cli_agent.session.ports import SessionKey, SessionRecord, SessionScope, SessionStore
from slack_cli_agent.session.store import SqliteSessionStore


def stored(store: SessionStore, session_key: SessionKey) -> SessionRecord:
    """get 이 None 을 내면 그 자리에서 실패시킨다."""
    record = store.get(session_key)
    assert record is not None
    return record


def key(scope: str = SessionScope.THREAD, k: str = "T1") -> SessionKey:
    return SessionKey(scope=scope, key=k)


@pytest.fixture
def store(database) -> SqliteSessionStore:
    return SqliteSessionStore(database)


class TestSqliteSessionStoreContract:
    def test_구현이_저장소_계약을_만족한다(self, store: SqliteSessionStore) -> None:
        assert isinstance(store, SessionStore)


class TestSqliteSessionStoreGetPut:
    def test_없는_키는_None(self, store: SqliteSessionStore) -> None:
        assert store.get(key()) is None

    def test_저장한_기록이_그대로_복원된다(self, store: SqliteSessionStore) -> None:
        record = SessionRecord(
            scope=SessionScope.THREAD, key="T1", session_id="sid-1", engine="claude",
            created_at=100.0, last_seen_ts="", updated_at=100.0,
        )
        store.put(record)
        assert store.get(key()) == record

    def test_같은_키에_다시_put하면_덮어쓴다(self, store: SqliteSessionStore) -> None:
        store.put(SessionRecord(
            scope=SessionScope.THREAD, key="T1", session_id="sid-1", engine="claude",
            created_at=100.0, last_seen_ts="", updated_at=100.0,
        ))
        store.put(SessionRecord(
            scope=SessionScope.THREAD, key="T1", session_id="sid-2", engine="codex",
            created_at=100.0, last_seen_ts="", updated_at=200.0,
        ))
        got = store.get(key())
        assert got is not None
        assert got.session_id == "sid-2"
        assert got.engine == "codex"

    def test_스코프가_다르면_같은_key여도_별개(self, store: SqliteSessionStore) -> None:
        store.put(SessionRecord(
            scope=SessionScope.THREAD, key="C1", session_id="sid-thread", engine="claude",
            created_at=100.0, last_seen_ts="", updated_at=100.0,
        ))
        store.put(SessionRecord(
            scope=SessionScope.CHANNEL, key="C1", session_id="sid-channel", engine="claude",
            created_at=100.0, last_seen_ts="", updated_at=100.0,
        ))
        assert stored(store, key(SessionScope.THREAD, "C1")).session_id == "sid-thread"
        assert stored(store, key(SessionScope.CHANNEL, "C1")).session_id == "sid-channel"


class TestSqliteSessionStoreTouch:
    def test_touch는_마지막으로_본_시각만_바꾼다(self, store: SqliteSessionStore) -> None:
        store.put(SessionRecord(
            scope=SessionScope.THREAD, key="T1", session_id="sid-1", engine="claude",
            created_at=100.0, last_seen_ts="", updated_at=100.0,
        ))
        store.touch(key(), "1700000000.000100")
        got = store.get(key())
        assert got is not None
        assert got.last_seen_ts == "1700000000.000100"
        assert got.updated_at == 100.0

    def test_없는_키를_touch해도_예외를_내지_않는다(self, store: SqliteSessionStore) -> None:
        store.touch(key(), "1.1")
        assert store.get(key()) is None


class TestSqliteSessionStoreExpire:
    def test_기준시각_이전_기록만_지운다(self, store: SqliteSessionStore) -> None:
        store.put(SessionRecord(
            scope=SessionScope.THREAD, key="OLD", session_id="sid-old", engine="claude",
            created_at=100.0, last_seen_ts="", updated_at=100.0,
        ))
        store.put(SessionRecord(
            scope=SessionScope.THREAD, key="NEW", session_id="sid-new", engine="claude",
            created_at=900.0, last_seen_ts="", updated_at=900.0,
        ))
        removed = store.expire(before=500.0)
        assert removed == 1
        assert store.get(key(SessionScope.THREAD, "OLD")) is None
        assert store.get(key(SessionScope.THREAD, "NEW")) is not None


@pytest.fixture
def settings() -> RuntimeSettings:
    return RuntimeSettings(session_ttl_hours=24, channel_session_ttl_days=7)


@pytest.fixture
def manager(store: SqliteSessionStore, settings: RuntimeSettings) -> tuple[SessionManager, dict[str, float]]:
    clock = {"now": 1_000_000.0}
    ids = iter(["new-sid-1", "new-sid-2", "new-sid-3"])
    return SessionManager(
        store=store, settings=settings,
        now=lambda: clock["now"],
        new_session_id=lambda: next(ids),
    ), clock


class TestSessionManagerResolveNoRecord:
    def test_기록이_없으면_새_세션이고_전체_재구성이_필요하다(self, manager) -> None:
        mgr, _clock = manager
        decision = mgr.resolve(key(), engine="claude")
        assert decision.resume is False
        assert decision.session_id == "new-sid-1"
        assert decision.rebuild_full is True
        assert decision.after_ts is None

    def test_판정_후_저장소에_새_기록이_남는다(self, manager, store: SqliteSessionStore) -> None:
        mgr, _clock = manager
        mgr.resolve(key(), engine="claude")
        got = store.get(key())
        assert got is not None
        assert got.session_id == "new-sid-1"
        assert got.engine == "claude"


class TestSessionManagerResolveExisting:
    def test_TTL_안이면_이어간다(self, manager, store: SqliteSessionStore) -> None:
        mgr, clock = manager
        store.put(SessionRecord(
            scope=SessionScope.THREAD, key="T1", session_id="old-sid", engine="claude",
            created_at=clock["now"], last_seen_ts="1000.1", updated_at=clock["now"],
        ))
        clock["now"] += 3600  # 1시간 경과. 24시간 TTL 안
        decision = mgr.resolve(key(), engine="claude")
        assert decision.resume is True
        assert decision.session_id == "old-sid"
        assert decision.rebuild_full is False
        assert decision.after_ts == "1000.1"

    def test_TTL을_넘기면_새_세션이다(self, manager, store: SqliteSessionStore) -> None:
        mgr, clock = manager
        store.put(SessionRecord(
            scope=SessionScope.THREAD, key="T1", session_id="old-sid", engine="claude",
            created_at=clock["now"], last_seen_ts="1000.1", updated_at=clock["now"],
        ))
        clock["now"] += 25 * 3600  # 24시간 TTL 초과
        decision = mgr.resolve(key(), engine="claude")
        assert decision.resume is False
        assert decision.rebuild_full is True
        assert decision.session_id == "new-sid-1"

    def test_채널_스코프는_7일_TTL이다(self, manager, store: SqliteSessionStore) -> None:
        mgr, clock = manager
        ch_key = key(SessionScope.CHANNEL, "C1")
        store.put(SessionRecord(
            scope=SessionScope.CHANNEL, key="C1", session_id="old-sid", engine="claude",
            created_at=clock["now"], last_seen_ts="", updated_at=clock["now"],
        ))
        base = clock["now"]

        clock["now"] = base + 6 * 86400  # 6일 경과. 7일 TTL 안
        assert mgr.resolve(ch_key, engine="claude").resume is True

        # 갱신 없이 새 기록으로 다시 놓아 8일 경과 상태를 만든다.
        store.put(SessionRecord(
            scope=SessionScope.CHANNEL, key="C1", session_id="old-sid", engine="claude",
            created_at=base, last_seen_ts="", updated_at=base,
        ))
        clock["now"] = base + 8 * 86400  # 8일 경과. 7일 TTL 초과
        assert mgr.resolve(ch_key, engine="claude").resume is False

    def test_엔진이_다르면_이어가지_않는다(self, manager, store: SqliteSessionStore) -> None:
        mgr, clock = manager
        store.put(SessionRecord(
            scope=SessionScope.THREAD, key="T1", session_id="old-sid", engine="codex",
            created_at=clock["now"], last_seen_ts="", updated_at=clock["now"],
        ))
        decision = mgr.resolve(key(), engine="claude")
        assert decision.resume is False
        assert decision.rebuild_full is True

    def test_이어가면_TTL이_지금_시각으로_늦춰진다(self, manager, store: SqliteSessionStore) -> None:
        mgr, clock = manager
        store.put(SessionRecord(
            scope=SessionScope.THREAD, key="T1", session_id="old-sid", engine="claude",
            created_at=clock["now"], last_seen_ts="", updated_at=clock["now"],
        ))
        clock["now"] += 3600
        mgr.resolve(key(), engine="claude")
        got = store.get(key())
        assert got is not None
        assert got.updated_at == clock["now"]


class TestSessionManagerTouch:
    def test_touch가_저장소에_반영된다(self, manager, store: SqliteSessionStore) -> None:
        mgr, clock = manager
        store.put(SessionRecord(
            scope=SessionScope.THREAD, key="T1", session_id="old-sid", engine="claude",
            created_at=clock["now"], last_seen_ts="", updated_at=clock["now"],
        ))
        mgr.touch(key(), "1234.5678")
        assert stored(store, key()).last_seen_ts == "1234.5678"


class TestSessionManagerReset:
    def test_reset은_이어가기를_끊고_새_세션을_돌려준다(self, manager, store: SqliteSessionStore) -> None:
        mgr, clock = manager
        store.put(SessionRecord(
            scope=SessionScope.THREAD, key="T1", session_id="old-sid", engine="claude",
            created_at=clock["now"], last_seen_ts="1.1", updated_at=clock["now"],
        ))
        decision = mgr.reset(key(), engine="claude")
        assert decision.resume is False
        assert decision.rebuild_full is True
        assert decision.session_id == "new-sid-1"
        assert stored(store, key()).session_id == "new-sid-1"


class TestSessionManagerRetryPolicy:
    def test_실패하면_새_대화로_재시도한다(self, manager) -> None:
        mgr, _clock = manager
        assert mgr.should_retry_with_new_session("nonzero_exit") is True
        assert mgr.should_retry_with_new_session("bad_json") is True

    def test_한도_소진은_재시도하지_않는다(self, manager) -> None:
        mgr, _clock = manager
        assert mgr.should_retry_with_new_session("usage_limit") is False

    def test_보장_미충족도_재시도하지_않는다(self, manager) -> None:
        """엔진의 보장은 세션을 새로 열어도 그대로다. 같은 자리에서 또 막힌다
        (sca-5sc)."""
        mgr, _clock = manager
        assert mgr.should_retry_with_new_session("capability_unmet") is False


class TestSqliteSessionStoreReassignSessionId:
    """실행기가 새로 발급한 세션 ID로 갈아 끼운다. 원본 persist_runner_session 이식.

    WHERE 절에 session_id·engine 을 함께 건다. 그 사이 다른 요청이 이미 세션을
    바꿔 놓았으면 지금 갱신이 그 변경을 덮어쓰면 안 되기 때문이다.
    """

    def test_기대와_일치하면_갱신하고_참을_돌려준다(self, store: SqliteSessionStore) -> None:
        store.put(SessionRecord(
            scope=SessionScope.THREAD, key="T1", session_id="tmp-sid", engine="codex",
            created_at=100.0, last_seen_ts="", updated_at=100.0,
        ))
        ok = store.reassign_session_id(
            key(), expected_session_id="tmp-sid", engine="codex",
            actual_session_id="real-sid", now=200.0,
        )
        assert ok is True
        got = store.get(key())
        assert got is not None
        assert got.session_id == "real-sid"
        assert got.updated_at == 200.0

    def test_기대와_다르면_갱신하지_않고_거짓을_돌려준다(self, store: SqliteSessionStore) -> None:
        store.put(SessionRecord(
            scope=SessionScope.THREAD, key="T1", session_id="changed-by-other", engine="codex",
            created_at=100.0, last_seen_ts="", updated_at=100.0,
        ))
        ok = store.reassign_session_id(
            key(), expected_session_id="tmp-sid", engine="codex",
            actual_session_id="real-sid", now=200.0,
        )
        assert ok is False
        got = store.get(key())
        assert got is not None
        assert got.session_id == "changed-by-other"

    def test_엔진이_다르면_갱신하지_않는다(self, store: SqliteSessionStore) -> None:
        store.put(SessionRecord(
            scope=SessionScope.THREAD, key="T1", session_id="tmp-sid", engine="claude",
            created_at=100.0, last_seen_ts="", updated_at=100.0,
        ))
        ok = store.reassign_session_id(
            key(), expected_session_id="tmp-sid", engine="codex",
            actual_session_id="real-sid", now=200.0,
        )
        assert ok is False
        assert stored(store, key()).session_id == "tmp-sid"

    def test_없는_키면_거짓을_돌려준다(self, store: SqliteSessionStore) -> None:
        ok = store.reassign_session_id(
            key(), expected_session_id="tmp-sid", engine="codex",
            actual_session_id="real-sid", now=200.0,
        )
        assert ok is False


class TestSessionManager실제세션ID반영:
    """엔진이 스스로 발급한 세션 ID 를 매핑에 반영하는가.

    엔진에 따라 우리가 만든 임시 ID 가 아니라 엔진이 새로 발급한 ID 를
    돌려준다. 그것을 반영하지 않으면 다음 요청이 엔진이 모르는 ID 로
    이어받기를 시도해 대화 맥락이 끊긴다.
    """

    def test_실제_ID_로_바꾼다(self, manager) -> None:
        mgr, _clock = manager
        key = SessionKey(scope="thread", key="T1")
        decision = mgr.resolve(key, engine="e")
        assert mgr.adopt_engine_session(key, decision.session_id, "e", "진짜") is True
        assert mgr.resolve(key, engine="e").session_id == "진짜"

    def test_빈_값이면_아무것도_안_한다(self, manager) -> None:
        mgr, _clock = manager
        key = SessionKey(scope="thread", key="T1")
        decision = mgr.resolve(key, engine="e")
        assert mgr.adopt_engine_session(key, decision.session_id, "e", "") is False
        assert mgr.resolve(key, engine="e").session_id == decision.session_id

    def test_같은_값이면_아무것도_안_한다(self, manager) -> None:
        mgr, _clock = manager
        key = SessionKey(scope="thread", key="T1")
        decision = mgr.resolve(key, engine="e")
        assert mgr.adopt_engine_session(key, decision.session_id, "e", decision.session_id) is False
