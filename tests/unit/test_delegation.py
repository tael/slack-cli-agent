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
OWNER_TOOLS = ("Write", "Edit", "Bash", "NotebookEdit", "WebFetch", "mcp__jira__*", "mcp__gmail__*")
# 위임 턴은 소유자 도구가 아니라 채널 operator 와 같은 프로필 operator_tools 를 받는다 (A9)
OPERATOR_GRANT = ("Write", "Edit", "Bash", "NotebookEdit", "WebFetch", "mcp__jira__create_issue")

PROFILE = {
    "name": "guchi",
    "delegation_by": "mametchi",
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
    return DelegationLedger(path, self_user_id=lambda: me, own_name="guchi",
                            delegator="mametchi", clock=lambda: NOW)


def policy(tmp_path: Path, rows: object | None, channels: dict | None = None) -> AccessPolicy:
    (tmp_path / "c.json").write_text(json.dumps(channels or {"C1": {}}), encoding="utf-8")
    led = ledger(tmp_path, rows) if rows is not None else None
    return AccessPolicy(Profile.from_dict(PROFILE), ChannelRegistry(tmp_path / "c.json"), delegation=led)


def tools(pol: AccessPolicy) -> ToolPolicy:
    return ToolPolicy(BASE, OWNER_TOOLS, channel_tools=pol.channel_tools_for,
                      operator_level=pol.operator_level, operator_tools=OPERATOR_GRANT)


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

    def test_지정한_위임자가_적은_행만_위임이다(self, tmp_path: Path) -> None:
        """마메치 delegation.lookup 은 by == DELEGATOR 만 받는다. 자기 이름만
        거르면 같은 파일에 다른 봇이 적은 행도 위임으로 받는다."""
        assert ledger(tmp_path, [row(by="other_bot")]).lookup("C1", "2.0") is None

    def test_위임자가_자기_이름이면_위임이_아니다(self, tmp_path: Path) -> None:
        path = tmp_path / "d.json"
        path.write_text(json.dumps([row(by="guchi")]), encoding="utf-8")
        led = DelegationLedger(path, self_user_id=lambda: ME, own_name="guchi",
                               delegator="guchi", clock=lambda: NOW)
        assert led.lookup("C1", "2.0") is None

    def test_자기_ID를_모르면_위임이_아니다(self, tmp_path: Path) -> None:
        assert ledger(tmp_path, [row()], me="").lookup("C1", "2.0") is None

    def test_파일이_없거나_깨지면_위임이_아니다(self, tmp_path: Path) -> None:
        led = DelegationLedger(tmp_path / "none.json", self_user_id=lambda: ME, own_name="g",
                               delegator="mametchi")
        assert led.lookup("C1", "2.0") is None
        (tmp_path / "bad.json").write_text("{", encoding="utf-8")
        led = DelegationLedger(tmp_path / "bad.json", self_user_id=lambda: ME, own_name="g",
                               delegator="mametchi")
        assert led.lookup("C1", "2.0") is None
        assert ledger(tmp_path, {"not": "list"}).lookup("C1", "2.0") is None


class TestControllerPrincipal:
    def test_원장이_없으면_controller_는_접수만_되고_GENERAL_이다(self, tmp_path: Path) -> None:
        pol = policy(tmp_path, None)
        p = pol.principal_for("C1", CTRL, "2.0")
        assert p.trust is TrustLevel.GENERAL
        assert p.is_controller and not p.delegated
        assert pol.accepts_request(pol.principal_for("C_UNREG", CTRL, "2.0"))
        assert tools(pol).tool_list_for(p, asker_is_bot=True) == BASE
        assert pol.operator_level(p, is_bot=True) == ""

    def test_원장에_ts_가_없으면_GENERAL_조회_도구다(self, tmp_path: Path) -> None:
        pol = policy(tmp_path, [row(message_ts="9.9")])
        p = pol.principal_for("C1", CTRL, "2.0")
        assert p.trust is TrustLevel.GENERAL and not p.delegated
        assert tools(pol).tool_list_for(p, asker_is_bot=True) == BASE

    def test_원장에_있고_TTL_안이면_위임_도구를_받는다(self, tmp_path: Path) -> None:
        pol = policy(tmp_path, [row()])
        p = pol.principal_for("C1", CTRL, "2.0")
        assert p.delegated
        # 소유자 등급은 아니다. 모델·effort·최고권한은 채널 값 그대로다
        assert p.trust is TrustLevel.GENERAL
        assert pol.is_full_authority(pol.principal_for("D1", CTRL, "2.0")) is False
        assert pol.model_for(p) == "s"
        got = tools(pol).tool_list_for(p, asker_is_bot=True)
        assert "WebFetch" in got and "mcp__jira__create_issue" in got
        # 소유자 서버 와일드카드(메일, 지라 전체)는 위임 턴에도 가지 않는다
        assert "mcp__jira__*" not in got and "mcp__gmail__*" not in got
        # 위임 턴은 operator_full 이 없는 채널에서도 맨 실행 도구를 받는다
        # (2026-11-08 test-weather-simul 위임 턴이 셸 없이 끝난 건)
        assert {"Bash", "Edit", "Write"} <= set(got)
        assert pol.operator_level(p, is_bot=True) == "operator_full"
        # 봇이어도 원장이 보증한 메시지는 실행 턴이다
        assert tools(pol).lookups_for(p, asker_is_bot=True) == ()

    def test_operator_full_채널의_위임은_맨_실행_도구까지_받는다(self, tmp_path: Path) -> None:
        pol = policy(tmp_path, [row()], {"C1": {"operator_full": True}})
        got = tools(pol).tool_list_for(pol.principal_for("C1", CTRL, "2.0"), asker_is_bot=True)
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

    def test_위임자_이름을_읽는다(self) -> None:
        assert Profile.from_dict(PROFILE).delegation_by == "mametchi"

    def test_원장만_있고_위임자가_없으면_거부한다(self) -> None:
        import pytest

        from slack_cli_agent.config.profile import ConfigError

        data = {k: v for k, v in PROFILE.items() if k != "delegation_by"}
        with pytest.raises(ConfigError):
            Profile.from_dict({**data, "delegation_ledger": "~/x/delegations.json"})


class TestInputMark:
    def test_위임_턴은_지시로_따르는_표식을_받는다(self, tmp_path: Path) -> None:
        from slack_cli_agent.engine.base import EngineRequest

        req = EngineRequest(prompt="p", system_prompt="s", session_id=None, resume=False,
                            model="m", effort="low", workdir=tmp_path, delegated=True)
        mark = input_mark_for(req)
        assert mark not in (UNTRUSTED_INPUT_MARK, OWNER_INPUT_MARK)
        assert "위임" in mark


class TestWriteLag:
    """2026-11-08 15:36:57 위임 글을 15:36:59.8 에 판정하고 원장은 15:37:01.6 에
    적혀, 위임 턴이 조회 전용으로 돌아 구치파치가 권한 없다며 포기했다."""

    def _setup(self, tmp_path: Path, posted: float, write_at: float | None):
        path = tmp_path / "delegations.json"
        path.write_text("[]", encoding="utf-8")
        clock = {"t": posted + 2.8}
        sleeps: list[float] = []

        def sleep(sec: float) -> None:
            sleeps.append(sec)
            clock["t"] += sec
            if write_at is not None and clock["t"] >= write_at and path.read_text() == "[]":
                path.write_text(json.dumps([row(message_ts=f"{posted:.6f}", created=write_at,
                                                expires=write_at + TTL_SEC)]), encoding="utf-8")

        led = DelegationLedger(path, self_user_id=lambda: ME, own_name="guchi", delegator="mametchi",
                               clock=lambda: clock["t"], sleep=sleep)
        return led, sleeps

    def test_waits_for_row_written_after_the_event(self, tmp_path: Path) -> None:
        posted = NOW
        led, sleeps = self._setup(tmp_path, posted, write_at=posted + 4.6)
        assert led.lookup("C1", f"{posted:.6f}") is None
        assert led.lookup_settled("C1", f"{posted:.6f}") is not None
        assert 0 < sum(sleeps) < 3

    def test_gives_up_after_grace(self, tmp_path: Path) -> None:
        posted = NOW
        led, sleeps = self._setup(tmp_path, posted, write_at=None)
        assert led.lookup_settled("C1", f"{posted:.6f}") is None
        assert sum(sleeps) <= 20

    def test_old_message_is_not_waited(self, tmp_path: Path) -> None:
        posted = NOW
        led, sleeps = self._setup(tmp_path, posted, write_at=None)
        assert led.lookup_settled("C1", f"{posted - 300:.6f}") is None
        assert sleeps == []

    def test_policy_marks_delegated_after_lag(self, tmp_path: Path) -> None:
        posted = NOW
        led, _ = self._setup(tmp_path, posted, write_at=posted + 4.6)
        (tmp_path / "c.json").write_text(json.dumps({"C1": {}}), encoding="utf-8")
        pol = AccessPolicy(Profile.from_dict(PROFILE), ChannelRegistry(tmp_path / "c.json"), delegation=led)
        assert pol.principal_for("C1", CTRL, f"{posted:.6f}").delegated is True
