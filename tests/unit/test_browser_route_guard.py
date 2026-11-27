"""브라우저 도구로 다른 수단이 정해진 호스트를 열면 막는지 (2026-11-27 private-taeil ERP 연차 기안)."""

from __future__ import annotations

import json
from pathlib import Path

from slack_cli_agent.guard.pretool.browser_route_guard import find_route, load_routes


def _routes(tmp_path: Path) -> list[dict]:
    (tmp_path / "browser_routes.json").write_text(json.dumps([
        {"hosts": ["erp.vroong.com", "uc.vroong.com"], "use": "aside exec"},
    ]))
    return load_routes(tmp_path)


def test_지정_호스트를_브라우저로_열면_막는다(tmp_path: Path) -> None:
    routes = _routes(tmp_path)
    hit = find_route("mcp__browser__browser_navigate", {"url": "https://erp.vroong.com/"}, routes)
    assert hit and hit["use"] == "aside exec"
    assert find_route("mcp__browser__browser_navigate", {"url": "https://uc.vroong.com/#/popup?x=1"}, routes)


def test_다른_호스트나_브라우저_밖_도구는_통과(tmp_path: Path) -> None:
    routes = _routes(tmp_path)
    assert find_route("mcp__browser__browser_navigate", {"url": "https://kibana.example.com"}, routes) is None
    assert find_route("mcp__browser__browser_navigate", {"url": "https://notuc.vroong.com.evil.io"}, routes) is None
    assert find_route("mcp__slack__slack_search", {"query": "erp.vroong.com"}, routes) is None


def test_설정_파일이_없으면_통과(tmp_path: Path) -> None:
    assert load_routes(tmp_path) == []
    assert find_route("mcp__browser__browser_navigate", {"url": "https://erp.vroong.com"}, []) is None
