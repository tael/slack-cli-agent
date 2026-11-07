"""A8 — 소유자 턴에서 다른 봇을 부른 게시 메시지 ts 를 위임 원장에 적는다.

읽는 쪽은 A7 (DelegationLedger) 이다. 적는 형식은 마메치 delegation.record 와
같다. 소유자 턴이 아니거나 수신자가 봇이 아니면 적지 않는다.
"""

from __future__ import annotations

import json
from pathlib import Path

from test_pipeline import build_pipeline, make_ctx, ok_response

from slack_cli_agent.auth.delegation import TTL_SEC, DelegationLedger, DelegationRecorder
from slack_cli_agent.config.profile import Profile

NOW = 1_800_000_000.0
POSTED = ("1700000000.000900", "1700000000.000901")


def recorder(tmp_path: Path, name: str = "mametchi") -> DelegationRecorder:
    return DelegationRecorder(tmp_path / "delegations.json", delegator=name, clock=lambda: NOW)


def rows(tmp_path: Path) -> list[dict]:
    path = tmp_path / "delegations.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


class Test기록자:
    def test_마메치_형식으로_ts_마다_한_줄을_적는다(self, tmp_path: Path) -> None:
        n = recorder(tmp_path).record("C1", "1.0", "1.1", POSTED, "UBOT", scope="일")
        assert n == 2
        got = rows(tmp_path)
        assert [r["message_ts"] for r in got] == list(POSTED)
        assert got[0] == {
            "channel": "C1", "thread_ts": "1.0", "origin_ts": "1.1",
            "message_ts": POSTED[0], "to": "UBOT", "by": "mametchi",
            "scope": "일", "created": NOW, "expires": NOW + TTL_SEC,
        }
        assert TTL_SEC == 1800

    def test_만료된_줄은_적을_때_지운다(self, tmp_path: Path) -> None:
        old = {"message_ts": "0.1", "by": "mametchi", "expires": NOW - 1}
        live = {"message_ts": "0.2", "by": "mametchi", "expires": NOW + 10}
        (tmp_path / "delegations.json").write_text(json.dumps([old, live]), encoding="utf-8")
        recorder(tmp_path).record("C1", "1.0", "1.1", ("2.0",), "UBOT")
        assert [r["message_ts"] for r in rows(tmp_path)] == ["0.2", "2.0"]

    def test_ts_나_수신자가_없으면_적지_않는다(self, tmp_path: Path) -> None:
        assert recorder(tmp_path).record("C1", "1.0", "1.1", (), "UBOT") == 0
        assert recorder(tmp_path).record("C1", "1.0", "1.1", ("2.0",), "") == 0
        assert rows(tmp_path) == []

    def test_scope_는_500자로_자른다(self, tmp_path: Path) -> None:
        recorder(tmp_path).record("C1", "1.0", "1.1", ("2.0",), "UBOT", scope="가" * 900)
        assert len(rows(tmp_path)[0]["scope"]) == 500

    def test_적은_줄을_받는_봇의_원장_판정이_읽는다(self, tmp_path: Path) -> None:
        recorder(tmp_path).record("C1", "1.0", "1.1", ("2.0",), "UGUCHI")
        reader = DelegationLedger(tmp_path / "delegations.json", self_user_id=lambda: "UGUCHI",
                                  own_name="guchi", delegator="mametchi", clock=lambda: NOW + 60)
        assert reader.lookup("C1", "2.0") is not None
        assert reader.lookup("C1", "2.1") is None


class Test파이프라인_배선:
    def _run(self, tmp_path: Path, body: str, user: str = "UOWNER", bots: set[str] | None = None):
        rec = recorder(tmp_path)
        pipeline, deps = build_pipeline(
            responses=[ok_response(body=body)],
            asker_is_bot=lambda u: u in (bots if bots is not None else {"UBOT"}),
            delegation_recorder=rec,
            tmp_path=tmp_path,
        )
        deps["publisher"]._message_ts = POSTED
        ctx = make_ctx(user=user)
        pipeline.handle(ctx)
        return ctx, deps

    def test_소유자_턴의_봇_수신자_게시는_원장에_남는다(self, tmp_path: Path) -> None:
        ctx, deps = self._run(tmp_path, "이 건 처리해 주세요.\n[[RECIPIENT:BOT:UBOT]]")
        assert deps["publisher"].posted[0]["text"].startswith("<@UBOT>")
        got = rows(tmp_path)
        assert [r["message_ts"] for r in got] == list(POSTED)
        assert {r["to"] for r in got} == {"UBOT"}
        assert got[0]["channel"] == ctx.channel
        assert got[0]["thread_ts"] == ctx.thread_ts
        assert got[0]["origin_ts"] == ctx.ts
        assert got[0]["scope"].startswith("<@UBOT>")
        recorded = [i for i in deps["audit"].incidents if i["kind"] == "delegation_recorded"]
        assert len(recorded) == 1 and recorded[0]["to"] == "UBOT"

    def test_소유자에게_답하면_적지_않는다(self, tmp_path: Path) -> None:
        self._run(tmp_path, "보고드립니다.\n[[RECIPIENT:OWNER]]")
        assert rows(tmp_path) == []

    def test_소유자_답_본문에_봇_멘션만_있으면_적지_않는다(self, tmp_path: Path) -> None:
        self._run(tmp_path, "<@UBOT> 에게 물어보겠습니다.\n[[RECIPIENT:OWNER]]")
        assert rows(tmp_path) == []

    def test_봇이_아닌_BOT_표식은_적지_않는다(self, tmp_path: Path) -> None:
        self._run(tmp_path, "부탁합니다.\n[[RECIPIENT:BOT:UHUMAN]]", bots=set())
        assert rows(tmp_path) == []

    def test_비소유자_턴은_봇에게_답해도_적지_않는다(self, tmp_path: Path) -> None:
        # 봇이 부른 턴 (위임받은 턴 포함) 이 원장을 늘리면 위임이 사슬로 이어진다.
        self._run(tmp_path, "확인했습니다.\n[[RECIPIENT:ASKER]]", user="UBOT")
        assert rows(tmp_path) == []

    def test_원장_쓰기_실패는_답을_실패로_바꾸지_않는다(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        rec = DelegationRecorder(blocker / "delegations.json", delegator="mametchi", clock=lambda: NOW)
        pipeline, deps = build_pipeline(
            responses=[ok_response(body="해 주세요.\n[[RECIPIENT:BOT:UBOT]]")],
            asker_is_bot=lambda u: u == "UBOT",
            delegation_recorder=rec,
            tmp_path=tmp_path,
        )
        outcome = pipeline.handle(make_ctx(user="UOWNER"))
        assert outcome.ok is True
        assert len(deps["publisher"].posted) == 1


PROFILE_BASE = {
    "name": "mametchi",
    "primary_engine": {"type": "claude", "binary": "claude", "model": "s"},
    "owner_user_id": "UOWNER",
    "troubleshoot_channel": "C_T",
}


class Test프로필:

    def test_delegation_record_키를_경로로_읽는다(self) -> None:
        p = Profile.from_dict({**PROFILE_BASE, "delegation_record": "~/.mametchi/delegations.json"})
        assert p.delegation_record == Path("~/.mametchi/delegations.json").expanduser()

    def test_키가_없으면_기록하지_않는다(self) -> None:
        assert Profile.from_dict(PROFILE_BASE).delegation_record is None
