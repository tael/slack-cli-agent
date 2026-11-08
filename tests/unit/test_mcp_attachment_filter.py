"""S2 컨텍스트 다이어트: 이 턴이 쓸 수 없는 MCP 서버는 --mcp-config 에 붙이지 않는다.

관찰 지점은 build_command() 가 만드는 --mcp-config JSON 의 서버 이름과
footprint_for() 의 mcp_servers_attached 다.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from slack_cli_agent.config.profile import Profile
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.engine.base import EngineRequest
from slack_cli_agent.engine.claude import ClaudeEngine
from slack_cli_agent.engine.tool_selection import ToolSelection

SERVERS = ("notion", "jira", "slack", "snowflake")


def _engine() -> ClaudeEngine:
    profile = Profile.from_dict({
        "name": "bot",
        "primary_engine": {"type": "claude", "binary": "/usr/bin/claude", "model": "m"},
        "owner_user_id": "U-OWNER",
        "troubleshoot_channel": "C-TROUBLE",
        "mcp_servers": {name: {"command": f"run-{name}"} for name in SERVERS},
    })
    return ClaudeEngine(profile, RuntimeSettings())


def _request(tmp_path: Path, tools: ToolSelection) -> EngineRequest:
    return EngineRequest(
        prompt="p", system_prompt="s", session_id="11111111-1111-1111-1111-111111111111",
        resume=False, model="m", effort="medium", workdir=tmp_path, tools=tools,
    )


def _attached(cmd: list[str]) -> set[str]:
    if "--mcp-config" not in cmd:
        return set()
    return set(json.loads(cmd[cmd.index("--mcp-config") + 1])["mcpServers"])


def test_unrestricted_turn_keeps_every_server(tmp_path):
    cmd = _engine().build_command(_request(tmp_path, ToolSelection.unrestricted()))
    assert _attached(cmd) == set(SERVERS)


def test_allowlist_drops_servers_it_never_names(tmp_path):
    tools = ToolSelection.allow(["Read", "mcp__jira__search", "mcp__slack__read_thread"])
    cmd = _engine().build_command(_request(tmp_path, tools))
    assert _attached(cmd) == {"jira", "slack"}


def test_bare_server_name_in_allowlist_keeps_that_server(tmp_path):
    tools = ToolSelection.allow(["Read", "notion"])
    cmd = _engine().build_command(_request(tmp_path, tools))
    assert _attached(cmd) == {"notion"}


def test_wildcard_server_keeps_all(tmp_path):
    tools = ToolSelection.allow(["Read", "mcp__*__*"])
    cmd = _engine().build_command(_request(tmp_path, tools))
    assert _attached(cmd) == set(SERVERS)


def test_partial_wildcard_matches_by_pattern(tmp_path):
    tools = ToolSelection.allow(["Read", "mcp__snow*__*"])
    cmd = _engine().build_command(_request(tmp_path, tools))
    assert _attached(cmd) == {"snowflake"}


def test_allowlist_without_mcp_names_attaches_nothing(tmp_path):
    tools = ToolSelection.allow(["Read", "Grep"])
    cmd = _engine().build_command(_request(tmp_path, tools))
    assert "--mcp-config" not in cmd
    # 전역 설정 서버가 끼어들지 않도록 strict 는 남는다.
    assert "--strict-mcp-config" in cmd


def test_selection_narrows_unrestricted_turn(tmp_path):
    request = replace(
        _request(tmp_path, ToolSelection.unrestricted()),
        mcp_selection=frozenset({"slack", "snowflake", "unknown"}),
    )
    cmd = _engine().build_command(request)
    assert _attached(cmd) == {"slack", "snowflake"}


def test_selection_intersects_with_allowlist(tmp_path):
    tools = ToolSelection.allow(["mcp__jira__search", "mcp__slack__read_thread"])
    request = replace(_request(tmp_path, tools), mcp_selection=frozenset({"slack", "notion"}))
    cmd = _engine().build_command(request)
    assert _attached(cmd) == {"slack"}


def test_footprint_records_attached_count(tmp_path):
    engine = _engine()
    tools = ToolSelection.allow(["mcp__jira__search"])
    footprint = engine.footprint_for(_request(tmp_path, tools))
    assert footprint.mcp_servers_attached == 1
    assert footprint.as_audit_dict()["mcp_servers_attached"] == 1
    full = engine.footprint_for(_request(tmp_path, ToolSelection.unrestricted()))
    assert full.as_audit_dict()["mcp_servers_attached"] == len(SERVERS)
