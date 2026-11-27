"""PreToolUse guard: a browser tool opening a host the bot must reach another way denies.

2026-11-27 private-taeil. The ERP (erp/uc hosts) is logged in only in a
separate browser agent CLI; the bot's own headless browser lands on the login
page. A turn opened it with the browser MCP anyway and reported the login page
as a blocker instead of switching tools, though a guideline already named the
right tool.

No host is hardcoded here (packaging boundary): routes come from
``$SCA_STATE_DIR/browser_routes.json``, a list of
``{"hosts": [...], "use": "<tool to use instead>", "why": "<optional>"}``.
A missing or broken file means no routes. Applies only to MCP tools whose
name contains "browser"; any string in the tool input naming a listed host
(exact host or subdomain) denies.

Standard library only; never fails the tool call -- every path ends in exit 0.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

HOOK_SCRIPT = Path(__file__).resolve()

ROUTES_FILE = "browser_routes.json"
_HOST_RE = re.compile(r"(?:^|[^A-Za-z0-9.-])(?:[a-z][a-z0-9+.-]*://)?([A-Za-z0-9.-]+\.[A-Za-z]{2,})(?=[:/?#\s\"']|$)")


def load_routes(state_dir: Path | str | None) -> list[dict[str, Any]]:
    if not state_dir:
        return []
    try:
        data = json.loads((Path(state_dir) / ROUTES_FILE).read_text())
    except (OSError, ValueError):
        return []
    return [r for r in data if isinstance(r, dict) and r.get("hosts")] if isinstance(data, list) else []


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def find_route(tool_name: str, tool_input: Any, routes: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not routes or not tool_name.startswith("mcp__") or "browser" not in tool_name:
        return None
    hosts = {h.lower() for s in _strings(tool_input) for h in _HOST_RE.findall(s)}
    for route in routes:
        for target in (str(h).lower() for h in route.get("hosts", [])):
            if any(h == target or h.endswith("." + target) for h in hosts):
                return {**route, "host": target}
    return None


def main() -> int:
    try:
        data = json.load(sys.stdin)
        hit = find_route(
            str(data.get("tool_name", "")),
            data.get("tool_input") or {},
            load_routes(os.environ.get("SCA_STATE_DIR")),
        )
        if not hit:
            return 0
    except Exception:  # noqa: BLE001 - never fail the tool call
        return 0
    why = f" {hit['why']}" if hit.get("why") else ""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"{hit['host']} 는 이 브라우저로 열지 않는다. {hit.get('use', '지정된 다른 수단')} 로 처리한다.{why} "
                "로그인 화면을 블로커로 보고하지 않고 지정 수단으로 바로 전환한다."
            ),
        },
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
