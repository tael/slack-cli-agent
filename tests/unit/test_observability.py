"""감사 기록과 안내문 목록 시험."""

from __future__ import annotations

import json

import pytest

from slack_cli_agent.observability.audit import AuditLog, IncidentKind, normalize_kind
from slack_cli_agent.observability.notices import NoticeCatalog, NoticeKey


@pytest.fixture
def jsonl_path(tmp_path):
    return tmp_path / "audit.jsonl"


@pytest.fixture
def audit_log(database, jsonl_path) -> AuditLog:
    clock = {"now": 1_700_000_000.0}
    return AuditLog(database, jsonl_path, now=lambda: clock["now"]), clock


class TestAuditLogRecord:
    def test_DB에_한_건이_쌓인다(self, audit_log) -> None:
        log, _clock = audit_log
        log.record("split", channel="C1", thread_ts="T1", reason="too_long")
        rows = log._fetch_all("SELECT * FROM audit")
        assert len(rows) == 1
        assert rows[0]["kind"] == "split"
        assert rows[0]["channel"] == "C1"
        assert rows[0]["thread_ts"] == "T1"

    def test_payload에_추가필드가_그대로_담긴다(self, audit_log) -> None:
        log, _clock = audit_log
        log.record("split", channel="C1", thread_ts="T1", reason="too_long", parts=3)
        row = log._fetch_all("SELECT * FROM audit")[0]
        payload = json.loads(row["payload"])
        assert payload["reason"] == "too_long"
        assert payload["parts"] == 3

    def test_jsonl_파일에도_한_줄이_남는다(self, audit_log, jsonl_path) -> None:
        log, _clock = audit_log
        log.record("split", channel="C1", thread_ts="T1", reason="too_long")
        lines = jsonl_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["kind"] == "split"
        assert entry["channel"] == "C1"
        assert entry["reason"] == "too_long"

    def test_여러_건이_누적된다(self, audit_log, jsonl_path) -> None:
        log, _clock = audit_log
        log.record("split", channel="C1", thread_ts="T1")
        log.record("post_failed", channel="C1", thread_ts="T1")
        assert len(log._fetch_all("SELECT * FROM audit")) == 2
        assert len(jsonl_path.read_text(encoding="utf-8").splitlines()) == 2

    def test_기록_시각이_now로_찍힌다(self, audit_log) -> None:
        log, clock = audit_log
        log.record("split", channel="C1", thread_ts="T1")
        row = log._fetch_all("SELECT * FROM audit")[0]
        assert row["at"] == clock["now"]


class TestAuditLogRecordRequest:
    def test_요청_처리_기록에_필요_필드가_모두_담긴다(self, audit_log, jsonl_path) -> None:
        log, _clock = audit_log
        log.record_request(
            channel="C1", thread_ts="T1", message_ts="T1",
            session_id="sid-1", resumed=True, model="claude-x", effort="high",
            first_reaction_sec=1.2, queue_wait_sec=0.4, elapsed=12.3,
            usage={"input_tokens": 100, "output_tokens": 50}, ok=True,
        )
        row = log._fetch_all("SELECT * FROM audit")[0]
        assert row["kind"] == "request"
        payload = json.loads(row["payload"])
        for field in (
            "message_ts", "session_id", "resumed", "model", "effort",
            "first_reaction_sec", "queue_wait_sec", "elapsed", "usage", "ok",
        ):
            assert field in payload

        entry = json.loads(jsonl_path.read_text(encoding="utf-8").splitlines()[0])
        assert entry["model"] == "claude-x"
        assert entry["ok"] is True

    def test_사용자와_턴_수가_담긴다(self, audit_log, jsonl_path) -> None:
        log, _clock = audit_log
        log.record_request(
            channel="C1", thread_ts="T1", message_ts="T1",
            session_id="sid-1", resumed=True, model="claude-x", effort="high",
            elapsed=12.3, ok=True, user="U1", turns=3,
        )
        row = log._fetch_all("SELECT * FROM audit")[0]
        payload = json.loads(row["payload"])
        assert payload["user"] == "U1"
        assert payload["turns"] == 3

        entry = json.loads(jsonl_path.read_text(encoding="utf-8").splitlines()[0])
        assert entry["user"] == "U1"
        assert entry["turns"] == 3

    def test_턴_수를_안_주면_모름으로_None이_담긴다(self, audit_log) -> None:
        """0턴과 모름을 구분해야 한다 — codex 는 턴 수를 아예 안 낸다."""
        log, _clock = audit_log
        log.record_request(
            channel="C1", thread_ts="T1", message_ts="T1",
            session_id="sid-1", resumed=False, model="codex-x", effort="high",
            elapsed=1.0, ok=True,
        )
        payload = json.loads(log._fetch_all("SELECT * FROM audit")[0]["payload"])
        assert payload["turns"] is None
        assert payload["user"] == ""


class TestIncidentKind:
    def test_새_사건_종류가_문자열_리터럴과_같다(self) -> None:
        assert IncidentKind.LATE_ADDENDUM == "late_addendum"
        assert IncidentKind.WRONG_ADDRESSEE == "wrong_addressee"
        assert IncidentKind.REWRITE_LOSS == "rewrite_loss"
        assert IncidentKind.SILENT == "silent"

    def test_기록에_그대로_쓸_수_있다(self, audit_log) -> None:
        log, _clock = audit_log
        log.record(IncidentKind.LATE_ADDENDUM, channel="C1", thread_ts="T1", ok=True)
        row = log._fetch_all("SELECT * FROM audit")[0]
        assert row["kind"] == "late_addendum"

    def test_kind이_비어있으면_request로_본다(self) -> None:
        assert normalize_kind(None) == "request"
        assert normalize_kind("") == "request"
        assert normalize_kind("late_addendum") == "late_addendum"


class TestNoticeCatalog:
    @pytest.fixture
    def catalog(self) -> NoticeCatalog:
        return NoticeCatalog()

    def test_안내문이_아닌_일반_답변은_False(self, catalog: NoticeCatalog) -> None:
        assert catalog.is_notice("이건 실제 답변입니다.") is False

    def test_빈_문자열은_안내문이_아니다(self, catalog: NoticeCatalog) -> None:
        assert catalog.is_notice("") is False
        assert catalog.is_notice(None) is False

    def test_기본_안내문_전부가_인식된다(self, catalog: NoticeCatalog) -> None:
        for key_ in NoticeKey:
            text = catalog.render(key_)
            assert catalog.is_notice(text) is True

    def test_앞뒤_공백이_있어도_인식된다(self, catalog: NoticeCatalog) -> None:
        text = catalog.render(NoticeKey.BUSY)
        assert catalog.is_notice(f"  {text}  ") is True

    def test_render는_등록된_문구를_그대로_돌려준다(self, catalog: NoticeCatalog) -> None:
        assert catalog.render(NoticeKey.JOINED) == "이 채널에 등록했습니다. 이제 여기서 답합니다."

    def test_커스텀_문구로_덮어쓸_수_있다(self) -> None:
        catalog = NoticeCatalog(overrides={NoticeKey.BUSY: "다른 문구"})
        assert catalog.render(NoticeKey.BUSY) == "다른 문구"
        assert catalog.is_notice("다른 문구") is True
        # 기본 문구는 더 이상 이 인스턴스의 안내문 목록에 없다
        assert catalog.is_notice("앞 요청을 처리 중입니다. 끝나면 이어서 답하겠습니다.") is False

    def test_모르는_키를_render하면_예외(self, catalog: NoticeCatalog) -> None:
        with pytest.raises(KeyError):
            catalog.render("존재하지않는키")
