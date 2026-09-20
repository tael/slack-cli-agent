"""Whether the MCP servers a profile declares can actually start.

Lives outside `preflight` because two callers need the same answer: the
startup gate, and the recovery report sent after a disconnect. A laptop
that slept and woke can come back with a server missing, and that report
is the only place the owner would see it (bot.py:6857).
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

_SHEBANG_ENV_RE = re.compile(r"#!\s*/usr/bin/env\s+(\S+)")


def broken_mcp_servers(mcp_config: Path) -> list[str]:
    """Names of the declared servers that cannot start, with the reason.

    An absent config declares no servers, so nothing is broken. Callers that
    want to tell "none declared" from "all healthy" check the path first.
    """
    if not mcp_config.exists():
        return []
    broken = []
    try:
        servers = json.loads(mcp_config.read_text()).get("mcpServers", {})
    except (OSError, json.JSONDecodeError) as e:
        return [f"MCP 설정을 읽지 못했습니다 : {e}"]
    for name, cfg in servers.items():
        command = cfg.get("command")
        if not command:
            continue
        real = shutil.which(command) or command
        if not os.path.exists(real):
            broken.append(f"{name} : 실행 파일 없음")
            continue
        try:
            with open(real, "rb") as f:
                first = f.readline(256).decode("utf-8", "replace").strip()
        except OSError:
            continue
        m = _SHEBANG_ENV_RE.match(first)
        if m and not shutil.which(m.group(1)):
            broken.append(f"{name} : {m.group(1)} 를 PATH 에서 못 찾음")
    return broken
