"""A7 — controller_user_ids 는 접수만, 실행 도구는 위임 원장이 정한다."""

from __future__ import annotations

import json
from pathlib import Path

from slack_cli_agent.auth.delegation import TTL_SEC, DelegationLedger
from slack_cli_agent.auth.policy import AccessPolicy
from slack_cli_agent.auth.principal import TrustLevel
from slack_cli_agent.auth.tools import ToolPolicy
from slack_cli_agent.config.channel import ChannelRegistry
from slack_cli_agent.config.profile import Profile
from slack_cli_agent.engine.base import OWNER_INPUT_MARK, UNTRUSTED_INPUT_MARK, input_mark_for

OWNER = "U_OWNER"
CTRL = "U_CTRL"
ME = "U_ME"
NOW = 1_800_000_000.0
BASE = ("Read", "Grep", "Glob")
OWNER_TOOLS = ("Write", "Edit", "Bash", "NotebookEdit", "WebFetch", "mcp__jira__*")

PROFILE = {
    "name": "guchi",
    "primary_engine": {"type": "claude", "binary": "claude", "model": "s", "model_owner": "o"},
    "owner_user_id": OWNER,
    "controller_user_ids": [CTRL],
    "troubleshoot_channel": "C_T",
}


def row(**over: object) -> dict:
    base = {
        "channel": "C1", "thread_ts": "1.0", "origin_ts": "1.1", "message_ts": "2.0",
        "to": ME, "by": "mametchi", "scope": "x", "created": NOW - 60, "expires": NOW - 60 + TTL_SEC,
    }
    base.update(over)
    return base


def ledger(tmp_path: Path, rows: object, me: str = ME) -> DelegationLedger:
    path = tmp_path / "delegations.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return DelegationLedger(path, self_user_id=lambda: me, own_name="guchi", clock=lambda: NOW)


def policy(tmp_path: Path, rows: object | None, channels: dict | None = None) -> AccessPolicy:
    (tmp_path / "c.json").write_text(json.dumps(channels or {"C1": {}}), encoding="utf-8")
    led = ledger(tmp_path, rows) if rows is not None else None
    return AccessPolicy(Profile.from_dict(PROFILE), ChannelRegistry(tmp_path / "c.json"), delegation=led)


def tools(pol: AccessPolicy) -> ToolPolicy:
    return ToolPolicy(BASE, OWNER_TOOLS, channel_tools=pol.channel_tools_for,
                      operator_full=pol.operator_full_for)


class TestLedger:
    def test_유효한_행을_찾는다(self, tmp_path: Path) -> None:
        assert ledger(tmp_path, [row()]).lookup("C1", "2.0") is not None

    def test_만료된_행은_없는_것이다(self, tmp_path: Path) -> None:
        assert ledger(tmp_path, [row(expires=NOW - 1)]).lookup("C1", "2.0") is None

    def test_expires_를_늘려도_TTL_을_넘긴_행은_없는_것이다(self, tmp_path: Path) -> None:
        stale = row(created=NOW - TTL_SEC - 1, expires=NOW + 9999)
        assert ledger(tmp_path, [stale]).lookup("C1", "2.0") is None

    def test_채널_ts_수신자가_다르면_없는_것이다(self, tmp_path: Path) -> None:
        led = ledger(tmp_path, [row(channel="C2"), row(message_ts="3.0"), row(to="U_OTHER")])
        assert led.lookup("C1", "2.0") is None

    def test_자기_이름으로_적힌_행은_위임이_아니다(self, tmp_path: Path) -> None:
        assert ledger(tmp_path, [row(by="guchi"), row(by="")]).lookup("C1", "2.0") is None

    def test_자기_ID를_모르면_위임이_아니다(self, tmp_path: Path) -> None:
        assert ledger(tmp_path, [row()], me="").lookup("C1", "2.0") is None

    def test_파일이_없거나_깨지면_위임이_아니다(self, tmp_path: Path) -> None:
        led = DelegationLedger(tmp_path / "none.json", self_user_id=lambda: ME, own_name="g")
        assert led.lookup("C1", "2.0") is None
        (tmp_path / "bad.json").write_text("{", encoding="utf-8")
        led = DelegationLedger(tmp_path / "bad.json", self_user_id=lambda: ME, own_name="g")
        assert led.lookup("C1", "2.0") is None
        assert ledger(tmp_path, {"not": "list"}).lookup("C1", "2.0") is None


class TestControllerPrincipal:
    def test_원장이_없으면_controller_는_접수만_되고_GENERAL_이다(self, tmp_path: Path) -> None:
        pol = policy(tmp_path, None)
        p = pol.principal_for("C1", CTRL, "2.0")
        assert p.trust is TrustLevel.GENERAL
        assert p.is_controller and not p.delegated
        assert pol.accepts_request(pol.principal_for("C_UNREG", CTRL, "2.0"))
        assert tools(pol).tool_list_for(p) == BASE

    def test_원장에_ts_가_없으면_GENERAL_조회_도구다(self, tmp_path: Path) -> None:
        pol = policy(tmp_path, [row(message_ts="9.9")])
        p = pol.principal_for("C1", CTRL, "2.0")
        assert p.trust is TrustLevel.GENERAL and not p.delegated
        assert tools(pol).tool_list_for(p) == BASE

    def test_원장에_있고_TTL_안이면_위임_도구를_받는다(self, tmp_path: Path) -> None:
        pol = policy(tmp_path, [row()])
        p = pol.principal_for("C1", CTRL, "2.0")
        assert p.delegated
        # 소유자 등급은 아니다. 모델·effort·최고권한은 채널 값 그대로다
        assert p.trust is TrustLevel.GENERAL
        assert pol.is_full_authority(pol.principal_for("D1", CTRL, "2.0")) is False
        assert pol.model_for(p) == "s"
        got = tools(pol).tool_list_for(p)
        assert "WebFetch" in got and "mcp__jira__*" in got
        # 맨 실행 도구는 operator_full 채널에서만 열린다
        assert not {"Bash", "Edit", "Write", "NotebookEdit"} & set(got)

    def test_operator_full_채널의_위임은_맨_실행_도구까지_받는다(self, tmp_path: Path) -> None:
        pol = policy(tmp_path, [row()], {"C1": {"operator_full": True}})
        got = tools(pol).tool_list_for(pol.principal_for("C1", CTRL, "2.0"))
        assert {"Bash", "Edit", "Write"} <= set(got)

    def test_controller_가_아닌_사람은_원장에_있어도_위임이_아니다(self, tmp_path: Path) -> None:
        pol = policy(tmp_path, [row()])
        p = pol.principal_for("C1", "U_STRANGER", "2.0")
        assert not p.delegated and not p.is_controller

    def test_소유자는_그대로_OWNER_다(self, tmp_path: Path) -> None:
        pol = policy(tmp_path, [row()])
        assert pol.principal_for("C1", OWNER, "2.0").trust is TrustLevel.OWNER


class TestProfileKey:
    def test_delegation_ledger_경로를_읽는다(self) -> None:
        profile = Profile.from_dict({**PROFILE, "delegation_ledger": "~/x/delegations.json"})
        assert profile.delegation_ledger == Path("~/x/delegations.json").expanduser()

    def test_키가_없으면_None_이다(self) -> None:
        assert Profile.from_dict(PROFILE).delegation_ledger is None


class TestInputMark:
    def test_위임_턴은_지시로_따르는_표식을_받는다(self, tmp_path: Path) -> None:
        from slack_cli_agent.engine.base import EngineRequest

        req = EngineRequest(prompt="p", system_prompt="s", session_id=None, resume=False,
                            model="m", effort="low", workdir=tmp_path, delegated=True)
        mark = input_mark_for(req)
        assert mark not in (UNTRUSTED_INPUT_MARK, OWNER_INPUT_MARK)
        assert "위임" in mark
