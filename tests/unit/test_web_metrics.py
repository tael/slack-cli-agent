"""웹 콘솔 지표 수집. 신규 저장소(state.db + ChannelRegistry) 기준.

원본(mametchi-slack-bot/dashboard/metrics.py)과 최상위 키는 같게 두되, 신규
감사 기록에 없는 값(사용자 식별자·질문/답변 원문·비용·턴 수·도구 이름)은
None 이나 not_applicable 사유로 낸다. 0 으로 채우지 않는다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from slack_cli_agent.config.channel import ChannelRegistry
from slack_cli_agent.config.profile import Profile
from slack_cli_agent.jobs.ports import JobStatus
from slack_cli_agent.observability.audit import REQUEST_KIND, IncidentKind
from slack_cli_agent.storage.database import Database
from slack_cli_agent.web import metrics as metrics_module
from slack_cli_agent.web.metrics import MetricsCollector

# bots 는 뺐다. 봇 명부는 /api/bots 가 낸다 - 지표는 봇 하나만 보므로
# 여기 담으면 선택줄에 봇이 1개만 나온다.
TOP_LEVEL_KEYS = {
    "generated_at", "window_days", "bot", "snapshot", "last_answer_kst",
    "channels", "responsiveness", "reliability", "quality", "usage", "followup",
    "tools", "queue_wait", "usage_block", "session_total",
}


def make_profile(tmp_path: Path, *, engine: str = "claude") -> Profile:
    state_dir = tmp_path / "bot"
    data = {
        "name": "example",
        "primary_engine": {"type": engine, "binary": "bin", "model": "model-x",
                            "model_owner": "model-owner"},
        "state_dir": str(state_dir),
        "owner_user_id": "U1",
        "troubleshoot_channel": "C1",
    }
    return Profile.from_dict(data)


def open_db(profile: Profile) -> Database:
    db = Database(profile.paths.database)
    db.migrate()
    return db


# Sentinel so a caller can omit user/turns entirely — distinct from passing
# "" or None, which is what a post-sca-qi5.2 record looks like when the
# value itself is genuinely empty/unknown. Rows from before that change
# don't have the key at all, and tests need to represent both shapes.
_UNSET = object()


def insert_request(
    db: Database,
    *,
    at: float,
    channel: str,
    thread_ts: str = "1.1",
    elapsed: float = 1.0,
    ok: bool = True,
    resumed: bool = False,
    model: str = "model-x",
    effort: str = "medium",
    first_reaction_sec: float | None = None,
    queue_wait_sec: float | None = None,
    usage: dict[str, int] | None = None,
    failure: str = "",
    user: object = _UNSET,
    turns: object = _UNSET,
) -> None:
    payload: dict[str, object] = {
        "message_ts": thread_ts,
        "session_id": "sid",
        "resumed": resumed,
        "model": model,
        "effort": effort,
        "elapsed": elapsed,
        "ok": ok,
        "first_reaction_sec": first_reaction_sec,
        "queue_wait_sec": queue_wait_sec,
        "usage": usage,
    }
    if failure:
        payload["failure"] = failure
    if user is not _UNSET:
        payload["user"] = user
    if turns is not _UNSET:
        payload["turns"] = turns
    db.connect().execute(
        "INSERT INTO audit (at, kind, channel, thread_ts, payload) VALUES (?, ?, ?, ?, ?)",
        (at, REQUEST_KIND, channel, thread_ts, json.dumps(payload, ensure_ascii=False)),
    )


def insert_review(db: Database, *, kind: str, channel: str, target_ts: str, at: float) -> None:
    db.connect().execute(
        "INSERT INTO reviews (kind, channel, target_ts, at, result) VALUES (?, ?, ?, ?, ?)",
        (kind, channel, target_ts, at, json.dumps({"status": "완료"})),
    )


def insert_incident(
    db: Database,
    *,
    kind: str,
    at: float,
    channel: str = "C1",
    thread_ts: str = "1.1",
    **payload: object,
) -> None:
    db.connect().execute(
        "INSERT INTO audit (at, kind, channel, thread_ts, payload) VALUES (?, ?, ?, ?, ?)",
        (at, kind, channel, thread_ts, json.dumps(payload, ensure_ascii=False)),
    )


def insert_session(
    db: Database,
    *,
    scope: str,
    key: str,
    session_id: str,
    updated_at: float,
    created_at: float | None = None,
    model: str = "model-x",
    workdir: str = "",
) -> None:
    db.connect().execute(
        "INSERT INTO sessions (scope, key, session_id, engine, created_at, last_seen_ts,"
        " updated_at, workdir, model) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (scope, key, session_id, "claude", created_at or updated_at, "1.1",
         updated_at, workdir, model),
    )


def insert_job(db: Database, *, channel: str, status: str, message_ts: str) -> None:
    db.connect().execute(
        "INSERT INTO jobs (channel, thread_ts, message_ts, user_id, payload, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (channel, message_ts, message_ts, "U1", "{}", status, 0.0),
    )


def write_snapshot(profile: Profile, data: dict[str, object]) -> None:
    profile.paths.root.mkdir(parents=True, exist_ok=True)
    profile.paths.state_snapshot.write_text(json.dumps(data), encoding="utf-8")


class Test최상위_키:
    def test_원본과_같은_최상위_키를_낸다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        collector = MetricsCollector(profile, now=lambda: 1_000.0)
        result = collector.collect(days=7)
        assert set(result.keys()) == TOP_LEVEL_KEYS

    def test_window_days_가_그대로_돌아온다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        collector = MetricsCollector(profile, now=lambda: 1_000.0)
        result = collector.collect(days=3)
        assert result["window_days"] == 3

    def test_generated_at_은_주입한_시계_기준_KST_이다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        # 2020-01-01T00:00:00+09:00
        collector = MetricsCollector(profile, now=lambda: 1577804400.0)
        result = collector.collect(days=7)
        assert result["generated_at"] == "2020-01-01T00:00:00+09:00"


class Test스냅샷_신선도_규칙:
    """기준은 health_interval_sec(기본 30초)의 2.5배다. 봇이 그 주기로
    스냅샷을 다시 쓰므로 주기보다 짧은 기준을 쓰면 매 주기 일부 구간이
    '상태 모름' 으로 보인다."""

    def test_한_주기_안이면_상태를_안다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        write_snapshot(profile, {"written_at": 1_000.0, "pid": 123})
        collector = MetricsCollector(profile, now=lambda: 1_035.0)
        snapshot = collector.collect(days=7)["snapshot"]
        assert snapshot["available"] is True

    def test_두_주기_넘게_안_쓰면_상태_모름이다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        write_snapshot(profile, {"written_at": 1_000.0, "pid": 123})
        collector = MetricsCollector(profile, now=lambda: 1_200.0)
        snapshot = collector.collect(days=7)["snapshot"]
        assert snapshot["available"] is False
        assert "지났다" in str(snapshot["reason"])

    def test_스냅샷_파일이_없으면_상태_모름이다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        collector = MetricsCollector(profile, now=lambda: 1_000.0)
        snapshot = collector.collect(days=7)["snapshot"]
        assert snapshot["available"] is False
        assert snapshot["reason"] == "스냅샷 파일 없음"

    def test_마지막_값을_현재로_읽지_않는다(self, tmp_path: Path) -> None:
        """오래된 스냅샷의 pid 등 값은 그대로 실려도 available 판정은 별개다."""
        profile = make_profile(tmp_path)
        write_snapshot(profile, {"written_at": 1_000.0, "pid": 999, "inflight": 5})
        collector = MetricsCollector(profile, now=lambda: 5_000.0)
        snapshot = collector.collect(days=7)["snapshot"]
        assert snapshot["available"] is False
        assert snapshot["pid"] == 999


class Test응답성:
    def test_지연_중앙값과_건수를_낸다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        db = open_db(profile)
        for i, elapsed in enumerate([5.0, 15.0, 400.0]):
            insert_request(db, at=1_000.0 + i, channel="C1", elapsed=elapsed)
        collector = MetricsCollector(profile, now=lambda: 1_100.0)
        responsiveness = collector.collect(days=7)["responsiveness"]
        assert responsiveness["count"] == 3
        assert responsiveness["median_sec"] == 15.0
        assert responsiveness["slow_count"] == 1

    def test_창_밖의_요청은_빠진다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        db = open_db(profile)
        now = 10 * 86400.0
        insert_request(db, at=now - 8 * 86400, channel="C1", elapsed=5.0)
        insert_request(db, at=now - 1 * 86400, channel="C1", elapsed=9.0)
        collector = MetricsCollector(profile, now=lambda: now)
        responsiveness = collector.collect(days=7)["responsiveness"]
        assert responsiveness["count"] == 1
        assert responsiveness["median_sec"] == 9.0

    def test_첫_반응_표본은_계측된_요청만_센다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        db = open_db(profile)
        insert_request(db, at=1_000.0, channel="C1", first_reaction_sec=2.0)
        insert_request(db, at=1_001.0, channel="C1", first_reaction_sec=None)
        collector = MetricsCollector(profile, now=lambda: 1_100.0)
        first_reaction = collector.collect(days=7)["responsiveness"]["first_reaction"]
        assert first_reaction["count"] == 1
        assert first_reaction["sample_pct"] == 50.0


class Test신뢰성:
    def test_성공률과_실패_사유를_낸다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        db = open_db(profile)
        insert_request(db, at=1_000.0, channel="C1", ok=True)
        insert_request(db, at=1_001.0, channel="C1", ok=False, failure="엔진 실행 실패")
        collector = MetricsCollector(profile, now=lambda: 1_100.0)
        reliability = collector.collect(days=7)["reliability"]
        assert reliability["total"] == 2
        assert reliability["ok"] == 1
        assert reliability["failed"] == 1
        assert reliability["success_pct"] == 50.0
        assert reliability["recent_failures"][0]["reason"] == "엔진 실행 실패"
        assert reliability["recent_failures"][0]["channel"] == "C1"

    def test_감사_기록에_없는_값은_None이다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        collector = MetricsCollector(profile, now=lambda: 1_000.0)
        reliability = collector.collect(days=7)["reliability"]
        assert reliability["context_reset"] is None
        assert reliability["restarts"] is None
        assert reliability["incidents"] == []


class Test검수_원장_기반_품질:
    def test_부검_건수를_채널별로_센다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        db = open_db(profile)
        insert_request(db, at=1_000.0, channel="C1")
        insert_review(db, kind="postmortem", channel="C1", target_ts="1.1", at=1_000.0)
        insert_review(db, kind="postmortem", channel="C1", target_ts="1.2", at=1_001.0)
        insert_review(db, kind="format_review", channel="C1", target_ts="1.3", at=1_002.0)
        insert_review(db, kind="debug_trace", channel="C2", target_ts="1.4", at=1_003.0)
        collector = MetricsCollector(profile, now=lambda: 1_100.0)
        quality = collector.collect(days=7)["quality"]
        assert quality["postmortems_total"] == 2
        assert quality["format_reviews"] == 1
        assert quality["debug_traces"] == 1
        row = next(r for r in quality["postmortems_by_channel"] if r["channel"] == "C1")
        assert row["postmortems"] == 2
        assert row["requests"] == 1

    def test_정정_지식_파일의_항목수를_센다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        knowledge = profile.paths.knowledge
        knowledge.mkdir(parents=True)
        (knowledge / "_corrections.md").write_text(
            "- 첫 정정\n- 둘째 정정\n메모\n", encoding="utf-8",
        )
        collector = MetricsCollector(profile, now=lambda: 1_000.0)
        quality = collector.collect(days=7)["quality"]
        assert quality["corrections"] == 2

    def test_정정_파일이_없으면_None이다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        collector = MetricsCollector(profile, now=lambda: 1_000.0)
        quality = collector.collect(days=7)["quality"]
        assert quality["corrections"] is None

    def test_사건이_한_건도_없으면_0건이지_판정불가가_아니다(self, tmp_path: Path) -> None:
        """sca-qi5.1: request 만 있고 사건이 하나도 없는 창은 0건이다.

        None(판정불가)과 다르다 — 이 종류는 이제 계측되므로, 없다는 것 자체가
        실제로 안 일어났다는 뜻이다. tracked_since 가 없는 것으로 '아직 한 번도
        기록되지 않았다'와 구분한다(test_아직_한_번도_기록되지_않은_종류는_구분된다).
        """
        profile = make_profile(tmp_path)
        db = open_db(profile)
        insert_request(db, at=1_000.0, channel="C1")
        collector = MetricsCollector(profile, now=lambda: 1_100.0)
        quality = collector.collect(days=7)["quality"]
        assert quality["late_addendum"] == 0
        assert quality["wrong_addressee"] == 0
        assert quality["split_broken"] == 0
        assert quality["post_failed"] == 0
        assert quality["silent"] == 0
        assert quality["rewrites"] == 0


class Test사고_종류별_건수:
    """sca-qi5.1: split_broken/post_failed/late_addendum 등 새 사건 종류를 센다."""

    def test_각_종류를_따로_센다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        db = open_db(profile)
        insert_request(db, at=1_000.0, channel="C1")
        insert_incident(db, kind=IncidentKind.LATE_ADDENDUM, at=1_001.0, ok=True, added_chars=10)
        insert_incident(db, kind=IncidentKind.WRONG_ADDRESSEE, at=1_002.0, wrong_target="U9")
        insert_incident(db, kind=IncidentKind.WRONG_ADDRESSEE, at=1_003.0, wrong_target="U9")
        insert_incident(db, kind=IncidentKind.SPLIT_BROKEN, at=1_004.0)
        insert_incident(db, kind=IncidentKind.POST_FAILED, at=1_005.0)
        insert_incident(db, kind=IncidentKind.REWRITE_LOSS, at=1_006.0)
        insert_incident(db, kind=IncidentKind.SILENT, at=1_007.0)
        collector = MetricsCollector(profile, now=lambda: 1_100.0)
        quality = collector.collect(days=7)["quality"]
        assert quality["late_addendum"] == 1
        assert quality["wrong_addressee"] == 2
        assert quality["split_broken"] == 1
        assert quality["post_failed"] == 1
        assert quality["rewrites"] == 1
        assert quality["silent"] == 1

    def test_기준_기록은_사고로_안_센다(self, tmp_path: Path) -> None:
        """capability 는 엔진 실행마다 남는 기준 기록이라 사고가 아니다.
        REQUEST 만 빼고 세면 정상 트래픽이 사고 건수를 채운다."""
        profile = make_profile(tmp_path)
        db = open_db(profile)
        insert_request(db, at=1_000.0, channel="C1")
        insert_incident(db, kind=IncidentKind.CAPABILITY, at=1_001.0, engine="claude")
        collector = MetricsCollector(profile, now=lambda: 1_100.0)
        result = collector.collect(days=7)
        assert result["reliability"]["incidents"] == []
        assert "capability" not in result["quality"]

    def test_침묵_비율은_전체_요청_대비다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        db = open_db(profile)
        insert_request(db, at=1_000.0, channel="C1")
        insert_request(db, at=1_001.0, channel="C1")
        insert_request(db, at=1_002.0, channel="C1")
        insert_request(db, at=1_003.0, channel="C1")
        insert_incident(db, kind=IncidentKind.SILENT, at=1_004.0)
        collector = MetricsCollector(profile, now=lambda: 1_100.0)
        quality = collector.collect(days=7)["quality"]
        assert quality["silent"] == 1
        assert quality["silent_pct"] == 25.0

    def test_창_밖의_사건은_안_센다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        db = open_db(profile)
        insert_request(db, at=1_000.0, channel="C1")
        old_cutoff = 1_000.0 - 8 * 86400
        insert_incident(db, kind=IncidentKind.LATE_ADDENDUM, at=old_cutoff, ok=True)
        collector = MetricsCollector(profile, now=lambda: 1_000.0)
        quality = collector.collect(days=7)["quality"]
        assert quality["late_addendum"] == 0

    def test_아직_한_번도_기록되지_않은_종류는_구분된다(self, tmp_path: Path) -> None:
        """sca-qi5.1 항목 5 — 새 종류는 오늘부터 기록되므로, 예전 audit.jsonl 에는
        아예 없다. 0건과 '아직 한 번도 안 찍힘'을 tracked_since 유무로 가른다.
        """
        profile = make_profile(tmp_path)
        db = open_db(profile)
        insert_request(db, at=1_000.0, channel="C1")
        collector = MetricsCollector(profile, now=lambda: 1_100.0)
        quality = collector.collect(days=7)["quality"]
        assert quality["tracked_since"]["late_addendum"] is None

    def test_한_번이라도_기록되면_그때부터_계측됐다고_본다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        db = open_db(profile)
        insert_request(db, at=1_000.0, channel="C1")
        insert_incident(db, kind=IncidentKind.LATE_ADDENDUM, at=1_050.0, ok=True)
        collector = MetricsCollector(profile, now=lambda: 1_100.0)
        quality = collector.collect(days=7)["quality"]
        assert quality["tracked_since"]["late_addendum"] is not None

    def test_사건_종류가_신뢰성_사고_유형에도_집계된다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        db = open_db(profile)
        insert_request(db, at=1_000.0, channel="C1")
        insert_incident(db, kind=IncidentKind.POST_FAILED, at=1_001.0)
        insert_incident(db, kind=IncidentKind.POST_FAILED, at=1_002.0)
        insert_incident(db, kind=IncidentKind.WRONG_ADDRESSEE, at=1_003.0)
        collector = MetricsCollector(profile, now=lambda: 1_100.0)
        reliability = collector.collect(days=7)["reliability"]
        by_kind = {row["kind"]: row["count"] for row in reliability["incidents"]}
        assert by_kind["post_failed"] == 2
        assert by_kind["wrong_addressee"] == 1
        assert "request" not in by_kind

    def test_사건이_없으면_신뢰성_사고_유형은_빈_목록이다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        db = open_db(profile)
        insert_request(db, at=1_000.0, channel="C1")
        collector = MetricsCollector(profile, now=lambda: 1_100.0)
        reliability = collector.collect(days=7)["reliability"]
        assert reliability["incidents"] == []

    def test_kind이_빈_레코드는_request로_본다(self, tmp_path: Path) -> None:
        """관측: record() 는 항상 kind 를 채우므로 정상 경로에선 안 일어난다.
        수기로 만졌거나 손상된 행을 대비한 방어적 기본값이다.
        """
        profile = make_profile(tmp_path)
        db = open_db(profile)
        db.connect().execute(
            "INSERT INTO audit (at, kind, channel, thread_ts, payload) VALUES (?, ?, ?, ?, ?)",
            (1_000.0, "", "C1", "1.1", json.dumps({
                "message_ts": "1.1", "session_id": "sid", "resumed": False,
                "model": "model-x", "effort": "medium", "elapsed": 1.0, "ok": True,
            })),
        )
        collector = MetricsCollector(profile, now=lambda: 1_100.0)
        result = collector.collect(days=7)
        assert result["reliability"]["total"] == 1


class Test사용_현황:
    def test_채널_모델_effort별_건수를_낸다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        db = open_db(profile)
        insert_request(db, at=1_000.0, channel="C1", model="claude-x", effort="high")
        insert_request(db, at=1_001.0, channel="C1", model="claude-x", effort="high")
        insert_request(db, at=1_002.0, channel="C2", model="claude-y", effort="low")
        collector = MetricsCollector(profile, now=lambda: 1_100.0)
        usage = collector.collect(days=7)["usage"]
        by_channel = {row["channel"]: row["count"] for row in usage["by_channel"]}
        assert by_channel == {"C1": 2, "C2": 1}
        by_model = {row["model"]: row["count"] for row in usage["by_model"]}
        assert by_model == {"claude-x": 2, "claude-y": 1}

    def test_토큰을_합산한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        db = open_db(profile)
        insert_request(db, at=1_000.0, channel="C1", usage={
            "input_tokens": 10, "output_tokens": 5,
            "cache_creation_tokens": 2, "cache_read_tokens": 3,
        })
        collector = MetricsCollector(profile, now=lambda: 1_100.0)
        usage = collector.collect(days=7)["usage"]
        assert usage["tokens"] == {
            "input": 10, "output": 5, "cache_write": 2, "cache_read": 3, "total": 20,
        }
        assert usage["tokens_sample"] == 1

    def test_사용자_식별자가_없어_by_user는_빈다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        db = open_db(profile)
        insert_request(db, at=1_000.0, channel="C1")
        collector = MetricsCollector(profile, now=lambda: 1_100.0)
        usage = collector.collect(days=7)["usage"]
        assert usage["by_user"] == []
        assert usage["cost_total_usd"] is None
        assert usage["turns_median"] is None
        assert usage["tracked_since"] == {"by_user": None, "turns_median": None}

    def test_사용자_필드가_기록되면_건수로_집계된다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        db = open_db(profile)
        insert_request(db, at=1_000.0, channel="C1", user="U1")
        insert_request(db, at=1_010.0, channel="C1", user="U1")
        insert_request(db, at=1_020.0, channel="C1", user="U2")
        collector = MetricsCollector(profile, now=lambda: 1_100.0)
        usage = collector.collect(days=7)["usage"]
        by_user = {row["user"]: row["count"] for row in usage["by_user"]}
        assert by_user == {"U1": 2, "U2": 1}
        assert usage["user_count"] == 2
        assert usage["tracked_since"]["by_user"] == metrics_module._kst(1_000.0)

    def test_사용자_필드가_빈_문자열이면_집계에서_빠진다(self, tmp_path: Path) -> None:
        """필드는 있지만 값이 비어 있는 요청(예: 식별 실패)까지 사용자로 세면 안 된다."""
        profile = make_profile(tmp_path)
        db = open_db(profile)
        insert_request(db, at=1_000.0, channel="C1", user="")
        collector = MetricsCollector(profile, now=lambda: 1_100.0)
        usage = collector.collect(days=7)["usage"]
        assert usage["by_user"] == []
        assert usage["user_count"] == 0
        # 필드 자체는 기록되기 시작했으므로 계측 시작 시각은 있다.
        assert usage["tracked_since"]["by_user"] == metrics_module._kst(1_000.0)

    def test_턴_수가_기록되면_중앙값을_낸다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        db = open_db(profile)
        insert_request(db, at=1_000.0, channel="C1", turns=1)
        insert_request(db, at=1_010.0, channel="C1", turns=3)
        insert_request(db, at=1_020.0, channel="C1", turns=5)
        collector = MetricsCollector(profile, now=lambda: 1_100.0)
        usage = collector.collect(days=7)["usage"]
        assert usage["turns_median"] == 3
        assert usage["tracked_since"]["turns_median"] == metrics_module._kst(1_000.0)

    def test_턴_수가_모름이면_중앙값_계산에서_빠진다(self, tmp_path: Path) -> None:
        """codex 처럼 턴 수를 안 내는 엔진의 요청은 turns 키가 null 로 남는다.

        필드 자체는 기록되기 시작했으니 계측 시작 시각은 있어도, 값이 없는
        건 0으로 세지 않고 중앙값 계산에서 제외해야 한다.
        """
        profile = make_profile(tmp_path)
        db = open_db(profile)
        insert_request(db, at=1_000.0, channel="C1", turns=None)
        insert_request(db, at=1_010.0, channel="C1", turns=4)
        collector = MetricsCollector(profile, now=lambda: 1_100.0)
        usage = collector.collect(days=7)["usage"]
        assert usage["turns_median"] == 4
        assert usage["tracked_since"]["turns_median"] == metrics_module._kst(1_000.0)

    def test_필드가_기록되기_시작하면_not_applicable에서_빠진다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        db = open_db(profile)
        insert_request(db, at=1_000.0, channel="C1", user="U1", turns=2)
        collector = MetricsCollector(profile, now=lambda: 1_100.0)
        result = collector.collect(days=7)
        not_applicable = result["bot"]["not_applicable"]
        assert "usage.by_user" not in not_applicable
        assert "usage.turns_median" not in not_applicable
        # 비용은 여전히 계산하지 않는다 — 단가를 코드에 못 박지 않기로 했다.
        assert "usage.cost_total_usd" in not_applicable

    def test_등록되지_않은_채널을_모은다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        db = open_db(profile)
        insert_request(db, at=1_000.0, channel="C9")
        collector = MetricsCollector(profile, now=lambda: 1_100.0)
        usage = collector.collect(days=7)["usage"]
        assert "C9" in usage["unlisted_channels"]


class Test후속_질문:
    def test_스레드당_단발과_복수_요청을_가른다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        db = open_db(profile)
        insert_request(db, at=1_000.0, channel="C1", thread_ts="1.1")
        insert_request(db, at=1_001.0, channel="C1", thread_ts="1.1")
        insert_request(db, at=1_002.0, channel="C1", thread_ts="1.2")
        collector = MetricsCollector(profile, now=lambda: 1_100.0)
        followup = collector.collect(days=7)["followup"]
        assert followup["threads"] == 2
        assert followup["single_turn"] == 1
        assert followup["multi_turn"] == 1
        assert followup["same_user_repeat"] is None


class Test큐_대기시간:
    def test_실측값의_분위수를_낸다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        db = open_db(profile)
        insert_request(db, at=1_000.0, channel="C1", queue_wait_sec=2.0)
        insert_request(db, at=1_001.0, channel="C1", queue_wait_sec=8.0)
        collector = MetricsCollector(profile, now=lambda: 1_100.0)
        queue_wait = collector.collect(days=7)["queue_wait"]
        assert queue_wait["count"] == 2
        assert queue_wait["median_sec"] == 8.0
        assert queue_wait["approximate"] is False

    def test_대기가_없던_요청만_있으면_0건이지_판정불가가_아니다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        db = open_db(profile)
        insert_request(db, at=1_000.0, channel="C1", queue_wait_sec=None)
        collector = MetricsCollector(profile, now=lambda: 1_100.0)
        queue_wait = collector.collect(days=7)["queue_wait"]
        assert queue_wait["count"] == 0
        assert queue_wait["median_sec"] is None


class Test채널별_행:
    def test_등록된_채널과_감사_기록의_채널을_모두_담는다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        registry = ChannelRegistry(profile.paths.channels)
        registry.update("C1", {"name": "일번 채널"})
        db = open_db(profile)
        insert_request(db, at=1_000.0, channel="C9")
        collector = MetricsCollector(profile, now=lambda: 1_100.0)
        rows = {r["channel"]: r for r in collector.collect(days=7)["channels"]}
        assert rows["C1"]["listed"] is True
        assert rows["C1"]["name"] == "일번 채널"
        assert rows["C9"]["listed"] is False

    def test_대기줄_건수는_jobs_표에서_읽는다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        db = open_db(profile)
        insert_job(db, channel="C1", status=JobStatus.QUEUED.value, message_ts="1.1")
        insert_job(db, channel="C1", status=JobStatus.QUEUED.value, message_ts="1.2")
        insert_job(db, channel="C1", status=JobStatus.RUNNING.value, message_ts="1.3")
        collector = MetricsCollector(profile, now=lambda: 1_100.0)
        rows = {r["channel"]: r for r in collector.collect(days=7)["channels"]}
        assert rows["C1"]["queued"] == 2

    def test_TTL_이내_세션만_살아있는_것으로_센다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        db = open_db(profile)
        now = 1_000_000.0
        # 24시간(THREAD_TTL) 이내
        insert_session(db, scope="thread", key="C1:1.1", session_id="s1",
                        updated_at=now - 3600)
        # 24시간을 넘겨 죽은 세션
        insert_session(db, scope="thread", key="C1:1.2", session_id="s2",
                        updated_at=now - 2 * 86400)
        insert_request(db, at=now - 100, channel="C1", thread_ts="1.1")
        insert_request(db, at=now - 200, channel="C1", thread_ts="1.2")
        collector = MetricsCollector(profile, now=lambda: now)
        rows = {r["channel"]: r for r in collector.collect(days=7)["channels"]}
        assert rows["C1"]["live_sessions"] == 1

    def test_채널_범위_세션은_키가_곧_채널이다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        db = open_db(profile)
        now = 1_000_000.0
        insert_session(db, scope="channel", key="C1", session_id="s1", updated_at=now - 10)
        collector = MetricsCollector(profile, now=lambda: now)
        rows = {r["channel"]: r for r in collector.collect(days=7)["channels"]}
        assert rows["C1"]["live_sessions"] == 1


class Test세션_총계:
    def test_TTL과_무관하게_모든_세션을_센다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        db = open_db(profile)
        now = 1_000_000.0
        insert_session(db, scope="thread", key="C1:1.1", session_id="s1",
                        updated_at=now - 100)
        insert_session(db, scope="thread", key="C1:1.2", session_id="s2",
                        updated_at=now - 30 * 86400)
        collector = MetricsCollector(profile, now=lambda: now)
        assert collector.collect(days=7)["session_total"] == 2


class Test사용량_블록:
    def test_codex_봇은_사용량_블록을_낼_수_없다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path, engine="codex")
        collector = MetricsCollector(profile, now=lambda: 1_000.0)
        usage_block = collector.collect(days=7)["usage_block"]
        assert usage_block["available"] is False

    def test_ccusage_실행파일이_없으면_사용_불가다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(metrics_module, "CCUSAGE_BIN", str(tmp_path / "없음"))
        profile = make_profile(tmp_path, engine="claude")
        collector = MetricsCollector(profile, now=lambda: 1_000.0)
        usage_block = collector.collect(days=7)["usage_block"]
        assert usage_block["available"] is False

    def test_ccusage_결과가_있으면_비용을_담는다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        binary = tmp_path / "ccusage"
        binary.write_text("", encoding="utf-8")
        monkeypatch.setattr(metrics_module, "CCUSAGE_BIN", str(binary))

        class FakeCompleted:
            returncode = 0
            stdout = json.dumps({
                "blocks": [{
                    "isActive": True,
                    "startTime": "2024-01-01T00:00:00Z",
                    "endTime": "2024-01-01T05:00:00Z",
                    "costUSD": 1.23,
                    "burnRate": {"costPerHour": 0.5},
                    "projection": {"totalCost": 2.0},
                    "totalTokens": 100,
                    "tokenCounts": {"cacheReadInputTokens": 10, "outputTokens": 20},
                    "entries": 3,
                    "models": ["claude-x"],
                }],
            })

        def fake_run(*args: object, **kwargs: object) -> FakeCompleted:
            return FakeCompleted()

        monkeypatch.setattr(metrics_module.subprocess, "run", fake_run)
        profile = make_profile(tmp_path, engine="claude")
        collector = MetricsCollector(profile, now=lambda: 1_000.0)
        usage_block = collector.collect(days=7)["usage_block"]
        assert usage_block["available"] is True
        assert usage_block["cost_usd"] == 1.23
        assert usage_block["kind"] == "ccusage_block"


class Test빈_저장소:
    def test_DB가_없어도_0건이지_예외가_아니다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        collector = MetricsCollector(profile, now=lambda: 1_000.0)
        result = collector.collect(days=7)
        assert result["session_total"] == 0
        assert result["responsiveness"]["count"] == 0
        assert result["reliability"]["total"] == 0

    def test_손상된_DB_페이로드는_건너뛴다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        db = open_db(profile)
        db.connect().execute(
            "INSERT INTO audit (at, kind, channel, thread_ts, payload)"
            " VALUES (?, ?, ?, ?, ?)",
            (1_000.0, REQUEST_KIND, "C1", "1.1", "이건 JSON이 아니다"),
        )
        collector = MetricsCollector(profile, now=lambda: 1_100.0)
        result = collector.collect(days=7)
        assert result["responsiveness"]["count"] == 0


class Test남지_않는_사유:
    def test_not_applicable_이유가_문자열로_있다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        collector = MetricsCollector(profile, now=lambda: 1_000.0)
        result = collector.collect(days=7)
        bot = result["bot"]
        assert isinstance(bot, dict)
        not_applicable = bot["not_applicable"]
        assert isinstance(not_applicable, dict)
        assert not_applicable
        for reason in not_applicable.values():
            assert isinstance(reason, str) and reason


class Test연결_정리:
    """지표 조회는 요청마다 DB 연결을 연다. 안 닫으면 콘솔이 5초마다
    폴링하면서 핸들이 늘어난다. 2026-09-15 에 5시간 만에 state.db 핸들
    127개가 열린 채로 콘솔이 응답을 못 하게 됐다."""

    def test_collect_가_끝나면_연결을_닫는다(self, tmp_path: Path, monkeypatch) -> None:
        opened: list[object] = []
        closed: list[object] = []

        real_close = metrics_module.Database.close

        def spy_close(self: object) -> None:
            closed.append(self)
            real_close(self)  # type: ignore[arg-type]

        real_init = metrics_module.Database.__init__

        def spy_init(self: object, path: Path) -> None:
            opened.append(self)
            real_init(self, path)  # type: ignore[arg-type]

        monkeypatch.setattr(metrics_module.Database, "__init__", spy_init)
        monkeypatch.setattr(metrics_module.Database, "close", spy_close)

        MetricsCollector(make_profile(tmp_path), now=lambda: 1_000.0).collect(days=7)

        assert opened, "collect 가 DB 를 열지 않았다"
        assert closed == opened, "연 만큼 닫지 않았다"


class Test정정지식두자리:
    """사람이 쓰는 자리와 학습이 쌓는 자리를 갈랐다(sca-jl4.5).

    새 줄은 학습 자리에 쌓이므로 사람 자리만 세면 그 날짜에서 수치가 끊긴다.
    """

    def test_두_자리의_항목을_더한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        for directory, text in (
            (profile.paths.knowledge, "- 옛 정정\n"),
            (profile.paths.learned, "- 새 정정\n- 또 하나\n"),
        ):
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "_corrections.md").write_text(text, encoding="utf-8")
        collector = MetricsCollector(profile, now=lambda: 1_000.0)
        assert collector.collect(days=7)["quality"]["corrections"] == 3

    def test_학습_자리만_있어도_센다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        profile.paths.learned.mkdir(parents=True, exist_ok=True)
        (profile.paths.learned / "_corrections.md").write_text("- 새 정정\n", encoding="utf-8")
        collector = MetricsCollector(profile, now=lambda: 1_000.0)
        assert collector.collect(days=7)["quality"]["corrections"] == 1
