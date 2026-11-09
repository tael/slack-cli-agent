"""위임한 봇이 받은 회신 글에만 실행 도구를 붙이는 동작 (마메치 delegation.lookup_reply).

마메치는 구치파치에게 일을 넘기고, 구치파치는 결과를 마메치에게 회신한다. 회신
턴이 읽기 전용이면 마메치가 후속 실행을 못 한다(2026-11-08 김태일 "마메치 턴도
권한이 있어야해"). 그렇다고 구치파치 원장의 아무 행이나 받으면 구치파치가 넘긴
일반 위임까지 열린다. 회신 행이고, 그 origin_ts 가 내가 그 봇에게 넘긴 글일 때만 연다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from slack_cli_agent.auth.delegation import TTL_SEC, DelegationLedger
from slack_cli_agent.auth.policy import AccessPolicy
from slack_cli_agent.config.channel import ChannelRegistry
from slack_cli_agent.config.profile import ConfigError, Profile

ME = "U_MAME"
PEER = "U_GUCHI"
NOW = 1_800_000_000.0


def peer_row(**over: object) -> dict:
    base = {
        "channel": "C1", "thread_ts": "1.0", "origin_ts": "1.5", "message_ts": "2.0",
        "to": ME, "by": "guchi", "kind": "reply", "scope": "x",
        "created": NOW - 60, "expires": NOW - 60 + TTL_SEC,
    }
    base.update(over)
    return base


def own_row(**over: object) -> dict:
    base = {
        "channel": "C1", "thread_ts": "1.0", "origin_ts": "1.2", "message_ts": "1.5",
        "to": PEER, "by": "mame", "scope": "y",
        "created": NOW - 600, "expires": NOW - 600 + TTL_SEC,
    }
    base.update(over)
    return base


def ledger(tmp_path: Path, peer_rows: list, own_rows: list | None) -> DelegationLedger:
    peer = tmp_path / "peer.json"
    peer.write_text(json.dumps(peer_rows), encoding="utf-8")
    own = None
    if own_rows is not None:
        own = tmp_path / "own.json"
        own.write_text(json.dumps(own_rows), encoding="utf-8")
    return DelegationLedger(
        peer, self_user_id=lambda: ME, own_name="mame", delegator="guchi",
        clock=lambda: NOW, reply_origin=own,
    )


class TestReplyOnly:
    def test_내가_넘긴_글의_회신이면_찾는다(self, tmp_path: Path) -> None:
        assert ledger(tmp_path, [peer_row()], [own_row()]).lookup("C1", "2.0", from_user=PEER) is not None

    def test_회신이_아닌_일반_위임_행은_받지_않는다(self, tmp_path: Path) -> None:
        plain = peer_row()
        del plain["kind"]
        assert ledger(tmp_path, [plain], [own_row()]).lookup("C1", "2.0", from_user=PEER) is None

    def test_내_원장에_없는_origin_이면_받지_않는다(self, tmp_path: Path) -> None:
        assert ledger(tmp_path, [peer_row(origin_ts="9.9")], [own_row()]).lookup(
            "C1", "2.0", from_user=PEER) is None

    def test_다른_봇에게_넘긴_글의_회신이면_받지_않는다(self, tmp_path: Path) -> None:
        assert ledger(tmp_path, [peer_row()], [own_row(to="U_OTHER")]).lookup(
            "C1", "2.0", from_user=PEER) is None

    def test_내_원장의_다른_by_행은_근거가_아니다(self, tmp_path: Path) -> None:
        assert ledger(tmp_path, [peer_row()], [own_row(by="someone")]).lookup(
            "C1", "2.0", from_user=PEER) is None

    def test_요청자를_모르면_받지_않는다(self, tmp_path: Path) -> None:
        assert ledger(tmp_path, [peer_row()], [own_row()]).lookup("C1", "2.0") is None

    def test_회신_모드가_아니면_종전대로_kind_를_보지_않는다(self, tmp_path: Path) -> None:
        plain = peer_row()
        del plain["kind"]
        assert ledger(tmp_path, [plain], None).lookup("C1", "2.0") is not None


PROFILE = {
    "name": "mame",
    "primary_engine": {"type": "claude", "binary": "claude", "model": "s"},
    "owner_user_id": "U_OWNER",
    "controller_user_ids": [PEER],
    "troubleshoot_channel": "C_T",
    "delegation_by": "guchi",
}


class TestProfile:
    def test_회신_모드는_내_기록_원장이_있어야_한다(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            Profile.from_dict({**PROFILE, "delegation_ledger": str(tmp_path / "p.json"),
                               "delegation_reply_only": True})

    def test_회신_모드_값을_읽는다(self, tmp_path: Path) -> None:
        p = Profile.from_dict({**PROFILE, "delegation_ledger": str(tmp_path / "p.json"),
                               "delegation_record": str(tmp_path / "o.json"),
                               "delegation_reply_only": True})
        assert p.delegation_reply_only is True

    def test_기본은_회신_모드가_아니다(self) -> None:
        assert Profile.from_dict(PROFILE).delegation_reply_only is False


class TestPolicy:
    def test_요청자를_원장_조회에_넘긴다(self, tmp_path: Path) -> None:
        (tmp_path / "c.json").write_text(json.dumps({"C1": {}}), encoding="utf-8")
        led = ledger(tmp_path, [peer_row()], [own_row()])
        pol = AccessPolicy(Profile.from_dict(PROFILE), ChannelRegistry(tmp_path / "c.json"), delegation=led)
        assert pol.principal_for("C1", PEER, message_ts="2.0").delegated is True


def test_Application_이_회신_모드에_내_기록_원장을_넘긴다(tmp_path: Path) -> None:
    from test_application import FakeSlackClient, write_profile

    from slack_cli_agent.core.application import Application

    peer = tmp_path / "peer.json"
    own = tmp_path / "own.json"
    peer.write_text(json.dumps([]), encoding="utf-8")
    profile = write_profile(
        tmp_path, delegation_ledger=str(peer), delegation_by="guchi",
        delegation_record=str(own), delegation_reply_only=True,
    )
    led = Application(profile, FakeSlackClient()).access_policy._delegation
    assert led is not None and led._reply_origin == own
