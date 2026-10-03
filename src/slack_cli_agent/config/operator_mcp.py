"""The operator's own Claude Code MCP servers, attached on owner turns.

A bot with `settings.inherit_operator_mcp` reads the user-scope `mcpServers`
of ~/.claude.json at boot, so the owner gets the tools of their own Claude
Code session without a second copy of that config in the profile (sca-kf6j).
Other turns stay on the profile's servers alone.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from ..core.errors import ConfigError
from .profile import McpServerSpec, Profile

log = logging.getLogger(__name__)

INHERIT_SETTING = "inherit_operator_mcp"


def operator_config_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base) / ".claude.json" if base else Path.home() / ".claude.json"


def operator_mcp_servers(path: Path) -> dict[str, McpServerSpec]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.warning("운영자 Claude Code 설정 파일이 없다 : %s - 물려받을 MCP 서버 없음", path)
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("운영자 Claude Code 설정 파일을 읽지 못했다 : %s (%s)", path, exc)
        return {}
    servers: dict[str, McpServerSpec] = {}
    for name, entry in (data.get("mcpServers") or {}).items():
        try:
            spec = McpServerSpec.from_dict(str(name), entry)
        except ConfigError as exc:
            log.warning("운영자 MCP 서버를 건너뛴다 : %s", exc)
            continue
        servers[str(name)] = replace(spec, owner_only=True)
    return servers


def inherit_operator_mcp(profile: Profile, path: Path | None = None) -> Profile:
    if not profile.settings_override.get(INHERIT_SETTING):
        return profile
    inherited = operator_mcp_servers(path or operator_config_path())
    shadowed = sorted(set(inherited) & set(profile.mcp_servers))
    if shadowed:
        log.info("프로필에 같은 이름이 있어 운영자 MCP 서버를 안 쓴다 : %s", ", ".join(shadowed))
    log.info("소유자 턴에 붙일 운영자 MCP 서버 : %s", ", ".join(sorted(set(inherited) - set(shadowed))) or "없음")
    return replace(profile, mcp_servers={**inherited, **profile.mcp_servers})


def owner_mcp_tool_patterns(mcp_servers: Mapping[str, McpServerSpec]) -> tuple[str, ...]:
    """Allowlist entries opening each owner-only server whole.

    --allowedTools takes mcp__<server>__* under dontAsk (measured 2026-10-03,
    claude 2.1.281, 10 servers and 180 tools, no ToolSearch needed).
    """
    return tuple(
        f"mcp__{name}__*" for name, spec in mcp_servers.items() if spec.owner_only and not spec.disabled
    )
