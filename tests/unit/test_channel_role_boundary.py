"""A9 검토 결함 재현 — 비소유자 역할 경계가 마메치보다 넓어지지 않는지 본다.

- operator 는 소유자 도구 전체가 아니라 프로필이 명시한 operator_tools 만 받는다
  (마메치 OWNER_EXTRA_TOOLS). 서버 와일드카드와 브라우저 조작 도구는 비소유자에게 가지 않는다
- codex 로 도는 비소유자 턴은 operator_full 채널에서도 read-only 샌드박스다
  (마메치 bot_profile.codex_sandbox 는 is_owner 만 본다. operator_full 예외가 없다)
- 서버 단위 허가는 표기와 무관하게 거른다. 맨 서버 이름('gmail'), 'mcp__kubernetes',
  'mcp__browser__*' 가 모두 서버 와일드카드다
- 봇 판정은 이벤트의 bot_id 로 한다. users_info 조회 실패와 무관하다 (bot.py:7855)
- claude 경로에서 범위를 좁힌 Bash 허가는 general 덧씌움의 맨 Bash deny 에 막히지 않는다
"""

from __future__ import annotations

import json
from pathlib import Path

import test_engine as engine_fixtures
from test_pipeline import build_pipeline, make_ctx, ok_response

from slack_cli_agent.auth.policy import AccessPolicy
from slack_cli_agent.auth.principal import TrustLevel
from slack_cli_agent.auth.tools import ToolPolicy
from slack_cli_agent.config.channel import ChannelRegistry
from slack_cli_agent.config.profile import Profile
from slack_cli_agent.engine.claude import ClaudeEngine
from slack_cli_agent.engine.codex import CodexEngine
from slack_cli_agent.engine.tool_selection import ToolSelection

OWNER = "U_OWNER"
OP = "U_OP"
CTL = "/opt/bot/airflow_ctl.sh"
BASE = ("Read", "Grep")
#: 구치파치 profiles/guchipachi.json owner_tools 와 같은 모양(서버 와일드카드).
PROFILE_OWNER_TOOLS = (
    "Write", "Edit", "Bash", "WebFetch",
    "mcp__gmail__*", "mcp__kubernetes__*", "mcp__browser__*", "mcp__slack__*",
)
OPERATOR_TOOLS = (
    "Bash", "Edit", "mcp__slack__slack_post_message", "mcp__notion__notion-create-pages",
    "mcp__gmail__*", "mcp__browser__browser_click",
)


def _policy(tmp_path: Path, channels: dict) -> AccessPolicy:
    profile = {
        "name": "example",
        "primary_engine": {"type": "claude", "binary": "claude", "model": "sonnet"},
        "owner_user_id": OWNER,
        "troubleshoot_channel": "C_TROUBLE",
        "airflow_ctl": CTL,
    }
    path = tmp_path / "c.json"
    path.write_text(json.dumps(channels), encoding="utf-8")
    return AccessPolicy(Profile.from_dict(profile), ChannelRegistry(path))


def _list(policy: AccessPolicy, channel: str, user: str, **kw) -> tuple[str, ...]:
    tools = ToolPolicy(
        BASE, PROFILE_OWNER_TOOLS,
        channel_tools=policy.channel_tools_for,
        operator_level=policy.operator_level,
        operator_tools=OPERATOR_TOOLS,
    )
    return tools.tool_list_for(policy.principal_for(channel, user), **kw)


class Test운영자는_명시목록만_받는다:
    def test_소유자_와일드카드는_operator에게_가지_않는다(self, tmp_path: Path) -> None:
        policy = _policy(tmp_path, {"C1": {"operators": ["*"], "operator_full": True}})
        붙은것 = set(_list(policy, "C1", OP))
        assert not {"mcp__gmail__*", "mcp__kubernetes__*", "mcp__browser__*", "mcp__slack__*"} & 붙은것
        assert {"mcp__slack__slack_post_message", "mcp__notion__notion-create-pages"} <= 붙은것

    def test_operator_tools의_와일드카드와_브라우저_조작은_빠진다(self, tmp_path: Path) -> None:
        policy = _policy(tmp_path, {"C1": {"operators": [OP]}})
        붙은것 = set(_list(policy, "C1", OP))
        assert "mcp__gmail__*" not in 붙은것
        assert "mcp__browser__browser_click" not in 붙은것
        assert not {"Bash", "Edit"} & 붙은것

    def test_operator_full이면_명시목록의_맨_쓰기도구가_돌아온다(self, tmp_path: Path) -> None:
        policy = _policy(tmp_path, {"C1": {"operators": [OP], "operator_full": True}})
        붙은것 = set(_list(policy, "C1", OP))
        assert {"Bash", "Edit"} <= 붙은것
        assert "Write" not in 붙은것  # operator_tools 에 없다

    def test_채널_extra_tools의_브라우저_조작도_비소유자에게_빠진다(self, tmp_path: Path) -> None:
        policy = _policy(tmp_path, {"C1": {"extra_tools": ["mcp__browser__browser_evaluate",
                                                            "mcp__browser__browser_snapshot"]}})
        붙은것 = _list(policy, "C1", "U_ANY")
        assert "mcp__browser__browser_evaluate" not in 붙은것
        assert "mcp__browser__browser_snapshot" in 붙은것
        assert "mcp__browser__browser_evaluate" in _list(policy, "C1", OWNER)

    def test_서버_단위_허가는_표기가_달라도_operator에게_가지_않는다(self, tmp_path: Path) -> None:
        policy = _policy(tmp_path, {"C1": {"operators": [OP], "operator_full": True}})
        tools = ToolPolicy(
            BASE, PROFILE_OWNER_TOOLS,
            channel_tools=policy.channel_tools_for, operator_level=policy.operator_level,
            operator_tools=("gmail", "mcp__kubernetes", "mcp__browser__browser_*", "mcp__*",
                            "mcp__slack__slack_post_message", "Bash"),
        )
        붙은것 = tools.tool_list_for(policy.principal_for("C1", OP))
        assert 붙은것 == ("Read", "Grep", "mcp__slack__slack_post_message", "Bash")

    def test_채널_extra_tools의_브라우저_서버_허가도_비소유자에게_빠진다(self, tmp_path: Path) -> None:
        extra = ["browser", "mcp__browser", "mcp__browser__*", "mcp__playwright__*", "mcp__*",
                 "mcp__browser__browser_snapshot"]
        policy = _policy(tmp_path, {"C1": {"extra_tools": extra}})
        붙은것 = _list(policy, "C1", "U_ANY")
        assert 붙은것 == ("Read", "Grep", "mcp__browser__browser_snapshot")
        assert set(extra) <= set(_list(policy, "C1", OWNER))

    def test_Application이_operator_tools_설정을_정책에_넘긴다(self, tmp_path: Path) -> None:
        from test_application import FakeSlackClient, write_profile

        from slack_cli_agent.auth.principal import Principal
        from slack_cli_agent.core.application import Application

        profile = write_profile(tmp_path, settings={
            "base_tools": ["Read"], "owner_tools": ["mcp__gmail__*"],
            "operator_tools": ["mcp__slack__slack_post_message"],
        })
        profile.paths.channels.parent.mkdir(parents=True, exist_ok=True)
        profile.paths.channels.write_text(json.dumps({"C1": {"operators": ["U1"]}}), encoding="utf-8")
        app = Application(profile, FakeSlackClient())
        general = Principal(user_id="U1", channel="C1", trust=TrustLevel.GENERAL, is_direct_message=False)
        assert app.tool_policy().tool_list_for(general) == ("Read", "mcp__slack__slack_post_message")


class Test대체엔진_codex_샌드박스:
    def _cmd(self, tmp_path: Path, trust: TrustLevel, names: list[str]) -> list[str]:
        engine = CodexEngine(engine_fixtures.codex_profile(tmp_path, {"sandbox": "workspace-write", "network": True}),
                             engine_fixtures.SETTINGS)
        return engine.build_command(engine_fixtures.request(
            model="gpt-5.6-sol", trust_level=trust, tools=ToolSelection.allow(names),
        ))

    def test_operator_도구가_있어도_비소유자는_read_only다(self, tmp_path: Path) -> None:
        for names in (["Read", "mcp__slack__slack_post_message"],
                      ["Read", f"Bash({CTL}:*)"],
                      ["Read", "mcp__notion__notion-create-pages"]):
            cmd = self._cmd(tmp_path, TrustLevel.GENERAL, names)
            assert 'sandbox_mode="read-only"' in cmd, names
            assert "--approve-for-me" not in cmd
            assert "sandbox_workspace_write.network_access=true" not in cmd

    def test_operator_full_채널의_비소유자도_read_only다(self, tmp_path: Path) -> None:
        # codex 는 허용목록을 강제하지 않고 프로필 MCP 서버를 모두 넘긴다. workspace-write 와
        # --approve-for-me 로 가면 gmail 발송, pods_delete, browser click 이 열린다.
        for trust, names in ((TrustLevel.GENERAL, ["Read", "Bash", "mcp__slack__slack_post_message"]),
                             (TrustLevel.GENERAL, ["Read", "Grep", "Bash"]),
                             (TrustLevel.TRUSTED, ["Read", "Edit", "Write", "Bash"])):
            cmd = self._cmd(tmp_path, trust, names)
            assert 'sandbox_mode="read-only"' in cmd, names
            assert "--approve-for-me" not in cmd
            assert "sandbox_workspace_write.network_access=true" not in cmd

    def test_명단_밖_사용자가_operator_full_채널_extra_tools로_Bash를_받아도_read_only다(
        self, tmp_path: Path,
    ) -> None:
        policy = _policy(tmp_path, {"C1": {"operator_full": True, "extra_tools": ["Bash"]}})
        names = list(_list(policy, "C1", "U_STRANGER"))
        assert "Bash" in names  # 도구 목록은 그대로다. 막는 것은 샌드박스다
        cmd = self._cmd(tmp_path, TrustLevel.GENERAL, names)
        assert 'sandbox_mode="read-only"' in cmd
        assert "--approve-for-me" not in cmd

    def test_소유자는_프로필_샌드박스다(self, tmp_path: Path) -> None:
        assert 'sandbox_mode="workspace-write"' in self._cmd(
            tmp_path, TrustLevel.OWNER, ["Read", "mcp__slack__slack_post_message"])


class Test봇판정은_이벤트로_한다:
    def test_조회가_실패해도_bot_id_이벤트는_operator가_되지_않는다(self) -> None:
        policy = ToolPolicy(
            base_tools=("Read",), owner_tools=(),
            operator_level=lambda principal, *, is_bot=False: "" if is_bot else "operator",
            operator_tools=("mcp__slack__slack_post_message",),
        )
        pipeline, parts = build_pipeline(
            responses=[ok_response(), ok_response()], tool_policy=policy,
            asker_is_bot=lambda user_id: False,  # users_info 조회 실패 = 사람으로 본다
        )
        pipeline.handle(make_ctx(user="UBOT", from_bot=True))
        assert parts["runner"].calls[0].tools.names == ("Read",)

    def test_listener가_bot_id를_요청에_싣는다(self) -> None:
        from slack_cli_agent.slack.listener import EventListener

        ctx = EventListener._context_from_event(
            _Listener(), {"channel": "C1", "user": "UB", "bot_id": "B1", "ts": "1.0", "text": "x"},
            unaddressed=False, is_dm=False,
        )
        assert ctx.from_bot is True
        assert EventListener._context_from_event(
            _Listener(), {"channel": "C1", "user": "U1", "ts": "1.0", "text": "x"},
            unaddressed=False, is_dm=False,
        ).from_bot is False


class _Listener:
    def _bot_user_id(self, bot_id: str) -> str:
        return ""


class Test범위지정_Bash는_설정축에서_막히지_않는다:
    def _deny(self, tmp_path: Path, names: list[str]) -> list[str]:
        profile = engine_fixtures.claude_profile(tmp_path)
        engine_fixtures.TestClaudeEngineBuildCommand.기본_settings를_쓴다(profile, {})
        engine_fixtures.TestClaudeEngineBuildCommand.덧씌움을_쓴다(
            profile, "general", {"permissions": {"deny": ["Bash", "Edit", "Write", "NotebookEdit"]}})
        cmd = ClaudeEngine(profile, engine_fixtures.SETTINGS).build_command(
            engine_fixtures.request(trust_level=TrustLevel.GENERAL, tools=ToolSelection.allow(names)))
        return json.loads(cmd[cmd.index("--settings") + 1])["permissions"]["deny"]

    def test_airflow_래퍼_허가가_있으면_맨_Bash_deny가_빠진다(self, tmp_path: Path) -> None:
        deny = self._deny(tmp_path, ["Read", f"Bash({CTL}:*)"])
        assert "Bash" not in deny
        assert {"Edit", "Write", "NotebookEdit"} <= set(deny)

    def test_범위지정_허가가_없으면_맨_Bash는_그대로_막힌다(self, tmp_path: Path) -> None:
        assert "Bash" in self._deny(tmp_path, ["Read", "mcp__slack__slack_post_message"])
