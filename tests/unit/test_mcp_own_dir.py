"""MCP 서버가 다른 봇 상태 디렉터리의 파일로 돌면 잡는다 (2026-11-14 구치파치 게시 도구 사례)."""

from __future__ import annotations

from pathlib import Path

from slack_cli_agent.config.profile import McpServerSpec
from slack_cli_agent.engine.mcp_health import foreign_bot_mcp_paths


def _spec(name: str, command: str, *args: str) -> McpServerSpec:
    return McpServerSpec.from_dict(name, {"command": command, "args": list(args)})


def test_다른_봇_디렉터리의_서버를_잡는다(tmp_path: Path) -> None:
    (tmp_path / ".mametchi" / "persona").mkdir(parents=True)
    (tmp_path / ".guchi" / "persona").mkdir(parents=True)
    servers = {
        "slack": _spec("slack", "/usr/bin/python3", str(tmp_path / ".mametchi/mcp/slack_readonly.py")),
        "own": _spec("own", "/usr/bin/python3", str(tmp_path / ".guchi/mcp/x.py")),
        "shared": _spec("shared", str(tmp_path / ".venv-global/bin/python"), "-V"),
    }
    found = foreign_bot_mcp_paths(servers, tmp_path / ".guchi", home=str(tmp_path))
    assert len(found) == 1 and found[0].startswith("slack :")


def test_persona_가_없는_공용_디렉터리는_통과한다(tmp_path: Path) -> None:
    (tmp_path / ".mcp").mkdir()
    servers = {"sf": _spec("sf", "/usr/bin/uvx", "--service-config-file", str(tmp_path / ".mcp/c.yaml"))}
    assert foreign_bot_mcp_paths(servers, tmp_path / ".guchi", home=str(tmp_path)) == []
