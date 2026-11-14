"""소유자 턴이 운영자 Claude Code 의 MCP 서버를 물려받는다 (sca-kf6j).

2026-10-03 사용자 지시 - 신지는 운영자 세션에서 할 수 있는 것을 할 수 있어야
한다. 운영자 설정을 프로필에 복사하면 두 곳이 되어 어긋나므로 기동 때
~/.claude.json 을 읽어 소유자 전용 서버로 붙인다. 비소유자 턴의 격리는
그대로다.
"""

from __future__ import annotations

import json
from pathlib import Path

from test_engine import profile_with, request
from test_engine_capability import SETTINGS

from slack_cli_agent.auth.principal import TrustLevel
from slack_cli_agent.config.operator_mcp import (
    inherit_operator_mcp,
    operator_mcp_servers,
    owner_mcp_tool_patterns,
)
from slack_cli_agent.engine.base import ToolSelection
from slack_cli_agent.engine.claude import ClaudeEngine
from slack_cli_agent.engine.codex import CodexEngine
from slack_cli_agent.engine.gemini import GeminiEngine

CLAUDE = {"type": "claude", "binary": "claude", "model": "claude-sonnet-5"}
CODEX = {"type": "codex", "binary": "codex", "model": "gpt-5.6-sol"}
GEMINI = {"type": "gemini", "binary": "agy", "model": "gemini-3.8-flash"}


def write_operator_config(home: Path, servers: dict) -> Path:
    path = home / ".claude.json"
    path.write_text(json.dumps({"mcpServers": servers, "projects": {}}), encoding="utf-8")
    return path


def with_flag(profile, enabled: bool = True):
    from dataclasses import replace

    return replace(profile, settings_override={"inherit_operator_mcp": enabled})


def mcp_config(cmd: list[str]) -> dict:
    if "--mcp-config" not in cmd:
        return {}
    return json.loads(cmd[cmd.index("--mcp-config") + 1])["mcpServers"]


class Test운영자_설정_판독:
    def test_사용자_범위_서버를_소유자_전용으로_읽는다(self, isolate_home: Path) -> None:
        path = write_operator_config(isolate_home, {
            "google-workspace": {"type": "stdio", "command": "uvx", "args": ["workspace-mcp"], "env": {"A": "1"}},
            "github": {"type": "http", "url": "https://example.test/mcp/", "headers": {"Authorization": "x"}},
        })
        servers = operator_mcp_servers(path)
        assert set(servers) == {"google-workspace", "github"}
        assert all(spec.owner_only for spec in servers.values())
        assert servers["google-workspace"].args == ("workspace-mcp",)
        assert servers["github"].is_remote

    def test_명령도_주소도_없는_항목은_건너뛴다(self, isolate_home: Path) -> None:
        path = write_operator_config(isolate_home, {"broken": {"type": "stdio"}, "ok": {"command": "x"}})
        assert set(operator_mcp_servers(path)) == {"ok"}

    def test_파일이_없으면_빈_결과다(self, isolate_home: Path) -> None:
        assert operator_mcp_servers(isolate_home / ".claude.json") == {}


class Test프로필_병합:
    def test_설정이_꺼져_있으면_그대로다(self, tmp_path: Path, isolate_home: Path) -> None:
        write_operator_config(isolate_home, {"google-workspace": {"command": "uvx"}})
        profile = with_flag(profile_with(CLAUDE, tmp_path=tmp_path), enabled=False)
        assert inherit_operator_mcp(profile) is profile

    def test_설정이_켜져_있으면_운영자_서버를_더한다(self, tmp_path: Path, isolate_home: Path) -> None:
        write_operator_config(isolate_home, {"google-workspace": {"command": "uvx"}})
        profile = inherit_operator_mcp(with_flag(profile_with(CLAUDE, tmp_path=tmp_path)))
        assert profile.mcp_servers["google-workspace"].owner_only

    def test_같은_이름이면_프로필_항목이_우선한다(self, tmp_path: Path, isolate_home: Path) -> None:
        write_operator_config(isolate_home, {"jira": {"command": "operator-jira"}})
        profile = with_flag(profile_with(CLAUDE, tmp_path=tmp_path, mcp_servers={"jira": {"command": "jira-mcp"}}))
        merged = inherit_operator_mcp(profile)
        assert merged.mcp_servers["jira"].command == "jira-mcp"
        assert not merged.mcp_servers["jira"].owner_only

    def test_허용목록에는_서버별_와일드카드를_넣는다(self, tmp_path: Path, isolate_home: Path) -> None:
        write_operator_config(isolate_home, {"google-workspace": {"command": "uvx"}})
        profile = inherit_operator_mcp(with_flag(profile_with(
            CLAUDE, tmp_path=tmp_path, mcp_servers={"jira": {"command": "jira-mcp"}},
        )))
        assert owner_mcp_tool_patterns(profile.mcp_servers) == ("mcp__google-workspace__*",)


def owner_profile(tmp_path: Path, primary: dict):
    from dataclasses import replace

    from slack_cli_agent.config.profile import McpServerSpec

    profile = profile_with(primary, tmp_path=tmp_path, mcp_servers={"jira": {"command": "jira-mcp"}})
    inherited = replace(McpServerSpec.from_dict("google-workspace", {"command": "uvx"}), owner_only=True)
    return replace(profile, mcp_servers={**profile.mcp_servers, "google-workspace": inherited})


OWNER_TOOLS = ToolSelection.allow(["Read", "mcp__google-workspace__*"])


class Test클로드_소유자_전용_서버:
    def test_소유자_턴에만_붙는다(self, tmp_path: Path) -> None:
        engine = ClaudeEngine(owner_profile(tmp_path, CLAUDE), SETTINGS)
        owner = engine.build_command(request(trust_level=TrustLevel.OWNER, tools=OWNER_TOOLS))
        general = engine.build_command(request(trust_level=TrustLevel.GENERAL))
        # 허용목록 턴은 허용 이름이 하나도 없는 서버를 붙이지 않으므로
        # (claude.py _attached_mcp_servers) 소유자 턴에는 와일드카드가 가리킨
        # 소유자 전용 서버만 남고, 어떤 MCP 이름도 없는 비소유자 턴에는
        # 프로필 서버도 붙지 않는다.
        assert set(mcp_config(owner)) == {"google-workspace"}
        assert set(mcp_config(general)) == set()
        assert "--strict-mcp-config" in owner and "--strict-mcp-config" in general

    def test_소유자_턴에서_와일드카드가_서버를_연다(self, tmp_path: Path) -> None:
        engine = ClaudeEngine(owner_profile(tmp_path, CLAUDE), SETTINGS)
        cmd = engine.build_command(request(trust_level=TrustLevel.OWNER, tools=OWNER_TOOLS))
        # ToolSearch 는 MCP 서버가 붙은 턴에 엔진이 더하는 이름이다.
        assert cmd[cmd.index("--allowedTools") + 1] == "Read,mcp__google-workspace__*,ToolSearch"
        assert cmd[cmd.index("--disallowedTools") + 1] == "mcp__jira__*"

    def test_비소유자_턴의_거부목록에_소유자_서버가_안_나온다(self, tmp_path: Path) -> None:
        """서버 자체가 안 붙으므로 거부 패턴으로 이름을 드러낼 필요도 없다."""
        engine = ClaudeEngine(owner_profile(tmp_path, CLAUDE), SETTINGS)
        cmd = engine.build_command(request(trust_level=TrustLevel.GENERAL))
        assert "google-workspace" not in cmd[cmd.index("--disallowedTools") + 1]


class Test다른_엔진의_소유자_전용_서버:
    def test_codex_는_소유자_턴에만_싣는다(self, tmp_path: Path) -> None:
        engine = CodexEngine(owner_profile(tmp_path, CODEX), SETTINGS)
        owner = " ".join(engine.build_command(request(trust_level=TrustLevel.OWNER, model="gpt-5.6-sol")))
        general = " ".join(engine.build_command(request(trust_level=TrustLevel.GENERAL, model="gpt-5.6-sol")))
        assert "mcp_servers.google-workspace." in owner
        assert "mcp_servers.google-workspace." not in general
        assert "mcp_servers.jira." in general

    def test_gemini_는_파일로_공유되므로_싣지_않는다(self, tmp_path: Path) -> None:
        engine = GeminiEngine(owner_profile(tmp_path, GEMINI), SETTINGS)
        workdir = tmp_path / "work"
        engine._ensure_mcp_config_file(workdir, engine.profile.mcp_servers)
        written = json.loads((workdir / ".agents" / "mcp_config.json").read_text(encoding="utf-8"))
        assert set(written["mcpServers"]) == {"jira"}


class Test조립:
    def test_Application_이_소유자_도구에_서버_와일드카드를_넣는다(self, tmp_path: Path, isolate_home: Path) -> None:
        """부품을 만든 것과 조립에 연결한 것은 다르다."""
        from test_application import FakeSlackClient, write_profile

        from slack_cli_agent.auth.principal import Principal
        from slack_cli_agent.core.application import Application

        write_operator_config(isolate_home, {"google-workspace": {"command": "uvx"}})
        profile = inherit_operator_mcp(write_profile(
            tmp_path, settings={"base_tools": ["Read"], "owner_tools": ["Write"], "inherit_operator_mcp": True},
        ))
        policy = Application(profile, FakeSlackClient()).tool_policy()
        owner = Principal(user_id="UOWNER", channel="C1", trust=TrustLevel.OWNER, is_direct_message=False)
        general = Principal(user_id="U1", channel="C1", trust=TrustLevel.GENERAL, is_direct_message=False)

        assert policy.tool_list_for(owner) == ("Read", "Write", "mcp__google-workspace__*")
        assert policy.tool_list_for(general) == ("Read",)

    def test_CLI_가_프로필을_읽을_때_운영자_서버를_붙인다(self, tmp_path: Path, isolate_home: Path) -> None:
        import argparse
        import io

        from slack_cli_agent import cli

        write_operator_config(isolate_home, {"google-workspace": {"command": "uvx"}})
        profile_dir = tmp_path / "profiles"
        profile_dir.mkdir()
        (profile_dir / "bot.json").write_text(json.dumps({
            "name": "bot", "primary_engine": CLAUDE, "owner_user_id": "U1", "troubleshoot_channel": "C1",
            "state_dir": str(tmp_path / "state"), "settings": {"inherit_operator_mcp": True},
        }), encoding="utf-8")
        seen = {}

        class Probe(cli.ProfileAwareCommand):
            name = "probe"

            def add_command_arguments(self, parser) -> None:
                pass

            def execute_with_profile(self, profile, args, stdout) -> int:
                seen["servers"] = profile.mcp_servers
                return 0

        Probe().execute(argparse.Namespace(profile="bot", profile_dir=[str(profile_dir)]), io.StringIO())
        assert seen["servers"]["google-workspace"].owner_only
