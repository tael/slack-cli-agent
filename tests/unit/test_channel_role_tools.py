"""A9 — 채널 역할 키(operators, operator_full, airflow_admins, extra_tools)를 도구 축에 연결한다.

기준은 마메치 bot.py build_command 의 분기다.
- operators 채널의 비소유자는 프로필의 operator_tools 를 받되 맨 Bash·Edit·Write·NotebookEdit 는 빠진다
- operator_full 채널의 operator 에게만 그 맨 이름이 돌아온다
- airflow_admins 에 적힌 사람은 airflow 래퍼 Bash 하나를 받는다
- extra_tools 는 그 채널의 턴에 그대로 붙는다
- 봇은 operator 가 되지 않는다. operator_tools 에 없는 소유자 도구는 operator 에게 가지 않는다
"""

from __future__ import annotations

import json
from pathlib import Path

from slack_cli_agent.auth.policy import AccessPolicy
from slack_cli_agent.auth.tools import ToolPolicy
from slack_cli_agent.config.channel import KNOWN_KEYS, ChannelConfig, ChannelRegistry
from slack_cli_agent.config.profile import Profile

OWNER = "U_OWNER"
OP = "U_OP"
OTHER = "U_OTHER"
CTL = "/opt/bot/airflow_ctl.sh"

BASE = ("Read", "Grep")
OWNER_TOOLS = ("Bash", "Edit", "Write", "WebFetch", "mcp__notion__*", "mcp__snowflake-rw__*")
OPERATOR_TOOLS = ("Bash", "Edit", "Write", "WebFetch", "mcp__notion__notion-create-pages")


def _policy(tmp_path: Path, channels: dict, *, airflow_ctl: str | None = CTL) -> AccessPolicy:
    profile = {
        "name": "example",
        "primary_engine": {"type": "claude", "binary": "claude", "model": "sonnet"},
        "owner_user_id": OWNER,
        "troubleshoot_channel": "C_TROUBLE",
    }
    if airflow_ctl:
        profile["airflow_ctl"] = airflow_ctl
    path = tmp_path / "c.json"
    path.write_text(json.dumps(channels), encoding="utf-8")
    return AccessPolicy(Profile.from_dict(profile), ChannelRegistry(path))


def _tools(policy: AccessPolicy) -> ToolPolicy:
    return ToolPolicy(
        BASE, OWNER_TOOLS,
        channel_tools=policy.channel_tools_for,
        operator_level=policy.operator_level,
        operator_tools=OPERATOR_TOOLS,
    )


def _list(policy: AccessPolicy, channel: str, user: str, **kw) -> tuple[str, ...]:
    return _tools(policy).tool_list_for(policy.principal_for(channel, user), **kw)


class Test선언필드:
    def test_역할키는_코어필드로_읽힌다(self) -> None:
        설정 = ChannelConfig.from_dict("C1", {
            "operators": ["U1", "*"], "airflow_admins": ["U3"], "extra_tools": ["WebSearch", " ", 3],
        })
        assert {"operators", "airflow_admins", "extra_tools"} <= KNOWN_KEYS
        assert 설정.operators == frozenset({"U1", "*"})
        assert 설정.airflow_admins == frozenset({"U3"})
        assert 설정.extra_tools == ("WebSearch",)
        assert 설정.extra == {}


class Test운영자도구:
    def test_operators_비소유자는_맨_쓰기도구를_뺀_소유자도구를_받는다(self, tmp_path: Path) -> None:
        policy = _policy(tmp_path, {"C1": {"operators": [OP]}})
        붙은것 = _list(policy, "C1", OP)
        assert "WebFetch" in 붙은것 and "mcp__notion__notion-create-pages" in 붙은것
        assert not {"Bash", "Edit", "Write"} & set(붙은것)

    def test_operator_full_채널에서만_맨_Bash_Edit_Write가_붙는다(self, tmp_path: Path) -> None:
        policy = _policy(tmp_path, {"C1": {"operators": [OP], "operator_full": True}})
        assert {"Bash", "Edit", "Write"} <= set(_list(policy, "C1", OP))

    def test_operator_tools에_없는_소유자도구는_operator_full에도_가지_않는다(self, tmp_path: Path) -> None:
        policy = _policy(tmp_path, {"C1": {"operators": ["*"], "operator_full": True}})
        assert "mcp__snowflake-rw__*" not in _list(policy, "C1", OP)

    def test_별표는_모두를_operator로_연다(self, tmp_path: Path) -> None:
        policy = _policy(tmp_path, {"C1": {"operators": ["*"]}})
        assert "mcp__notion__notion-create-pages" in _list(policy, "C1", OTHER)

    def test_명단에_없는_사람과_다른_채널은_기본도구뿐이다(self, tmp_path: Path) -> None:
        policy = _policy(tmp_path, {"C1": {"operators": [OP]}, "C2": {}})
        assert _list(policy, "C1", OTHER) == BASE
        assert _list(policy, "C2", OP) == BASE

    def test_봇은_operator가_되지_않는다(self, tmp_path: Path) -> None:
        policy = _policy(tmp_path, {"C1": {"operators": ["*"], "operator_full": True}})
        assert _list(policy, "C1", OP, asker_is_bot=True) == BASE

    def test_aside_턴에는_운영자도구가_붙지_않는다(self, tmp_path: Path) -> None:
        policy = _policy(tmp_path, {"C1": {"operators": [OP]}})
        assert _list(policy, "C1", OP, aside=True) == BASE

    def test_읽기전용_턴에는_쓰기도구가_남지_않는다(self, tmp_path: Path) -> None:
        policy = _policy(tmp_path, {"C1": {"operators": [OP], "operator_full": True}})
        붙은것 = _list(policy, "C1", OP, readonly=True)
        assert not {"Bash", "Edit", "Write", "mcp__notion__notion-create-pages"} & set(붙은것)


class TestAirflow관리자:
    def test_명단의_사람은_airflow_래퍼_Bash만_받는다(self, tmp_path: Path) -> None:
        policy = _policy(tmp_path, {"C1": {"airflow_admins": [OP]}})
        assert _list(policy, "C1", OP) == (*BASE, f"Bash({CTL}:*)")
        assert _list(policy, "C1", OTHER) == BASE

    def test_래퍼경로가_없는_프로필은_붙이지_않는다(self, tmp_path: Path) -> None:
        policy = _policy(tmp_path, {"C1": {"airflow_admins": [OP]}}, airflow_ctl=None)
        assert _list(policy, "C1", OP) == BASE


class Test채널추가도구:
    def test_누구에게나_그대로_붙는다(self, tmp_path: Path) -> None:
        policy = _policy(tmp_path, {"C1": {"extra_tools": ["mcp__openalex__*"]}})
        assert _list(policy, "C1", OTHER) == (*BASE, "mcp__openalex__*")

    def test_소유자에게도_붙는다(self, tmp_path: Path) -> None:
        policy = _policy(tmp_path, {"C1": {"extra_tools": ["mcp__openalex__*"]}})
        assert "mcp__openalex__*" in _list(policy, "C1", OWNER)

    def test_맨_쓰기도구는_operator_full_없이는_빠진다(self, tmp_path: Path) -> None:
        policy = _policy(tmp_path, {"C1": {"extra_tools": ["Bash", "mcp__notion__notion-create-pages"]}})
        assert _list(policy, "C1", OTHER) == (*BASE, "mcp__notion__notion-create-pages")
