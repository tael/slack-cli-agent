"""Whether the MCP servers a profile declares can actually start.

Lives outside `preflight` because two callers need the same answer: the
startup gate, and the recovery report sent after a disconnect. A laptop
that slept and woke can come back with a server missing, and that report
is the only place the owner would see it (bot.py:6857).

Reads the profile rather than a config file. The original checks the file
it hands to --mcp-config (bot.py:6548), but this port has no such file:
claude gets inline JSON, codex gets -c arguments, gemini gets a file in
the work directory. Reading a path nothing writes made the check pass
always.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping

from ..config.profile import McpServerSpec

_SHEBANG_ENV_RE = re.compile(r"#!\s*/usr/bin/env\s+(\S+)")


def broken_mcp_servers(mcp_servers: Mapping[str, McpServerSpec]) -> list[str]:
    """Names of the declared servers that cannot start, with the reason.

    Skips the ones no engine would attach: disabled servers, and remote
    ones, which have no local executable to look at.
    """
    broken = []
    for name, spec in mcp_servers.items():
        if spec.disabled or spec.is_remote or not spec.command:
            continue
        real = shutil.which(spec.command) or spec.command
        if not os.path.exists(real):
            broken.append(f"{name} : 실행 파일 없음")
            continue
        try:
            with open(real, "rb") as f:
                first = f.readline(256).decode("utf-8", "replace").strip()
        except OSError as e:
            broken.append(f"{name} : 실행 파일을 읽지 못했습니다 : {e}")
            continue
        m = _SHEBANG_ENV_RE.match(first)
        if m and not shutil.which(m.group(1)):
            broken.append(f"{name} : {m.group(1)} 를 PATH 에서 못 찾음")
    return broken


def foreign_bot_mcp_paths(
    mcp_servers: Mapping[str, McpServerSpec], state_dir: os.PathLike[str] | str, home: str | None = None,
) -> list[str]:
    """MCP servers that run code out of another bot's state directory.

    2026-11-14: guchipachi's posting tool was fixed in the mametchi copy
    (~/.mametchi/mcp) and the fix never reached the copy guchipachi runs; its
    openalex server and claude wrapper also ran from ~/.mametchi. One bot's
    tools must not depend on another bot's files. A home dot-directory with a
    persona/ subdirectory is treated as a bot state directory.
    """
    home_dir = os.path.realpath(home or os.path.expanduser("~"))
    own = os.path.realpath(os.path.expanduser(str(state_dir)))
    found = []
    for name, spec in mcp_servers.items():
        if spec.disabled or spec.is_remote:
            continue
        for raw in (spec.command, *spec.args):
            path = os.path.expanduser(str(raw))
            if not os.path.isabs(path):
                continue
            rel = os.path.relpath(os.path.realpath(path), home_dir)
            top = rel.split(os.sep, 1)[0]
            if rel.startswith("..") or not top.startswith("."):
                continue
            bot_dir = os.path.join(home_dir, top)
            if bot_dir != own and os.path.isdir(os.path.join(bot_dir, "persona")):
                found.append(f"{name} : 다른 봇 디렉터리 {bot_dir} 의 {path} 를 씀")
                break
    return found
