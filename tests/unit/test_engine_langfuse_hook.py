"""N2: langfuse_hook 키가 Stop·SessionEnd 훅을 붙이는 조건 (sca-blrc.2)."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from test_engine import SETTINGS, claude_profile, request

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.engine import claude as claude_module
from slack_cli_agent.engine.claude import (
    NO_ATTRIBUTION_SETTINGS,
    ClaudeEngine,
    guard_hook_settings,
)
from slack_cli_agent.engine.claude_settings import merge_settings


@pytest.fixture(autouse=True)
def _경고_기록_초기화() -> None:
    claude_module._LANGFUSE_WARNED.clear()


def _훅_명령(settings: dict[str, Any], event: str) -> list[str]:
    return [
        h["command"]
        for entry in settings.get("hooks", {}).get(event, [])
        for h in entry.get("hooks", [])
        if h.get("type") == "command"
    ]


def _엔진(
    tmp_path: Path, langfuse_hook: str = "", base: dict[str, Any] | None = None
) -> ClaudeEngine:
    profile = claude_profile(tmp_path)
    path = profile.paths.engine_settings("claude")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(base or {"permissions": {"deny": ["공통"]}}), encoding="utf-8")
    runtime = SETTINGS.override({"langfuse_hook": langfuse_hook}) if langfuse_hook else SETTINGS
    return ClaudeEngine(profile, runtime)


def _조각(engine: ClaudeEngine) -> dict[str, Any]:
    return engine._settings_value(request())


def _훅_파일(tmp_path: Path) -> Path:
    훅 = tmp_path / "hook.sh"
    훅.write_text("#!/bin/sh\n")
    return 훅


def test_기본값은_빈_문자열이다() -> None:
    assert RuntimeSettings().langfuse_hook == ""


def test_꺼짐이면_조각이_현재_출력과_같다(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    base = {"permissions": {"deny": ["공통"]}}
    기대 = merge_settings(base, guard_hook_settings(), NO_ATTRIBUTION_SETTINGS)
    with caplog.at_level(logging.WARNING):
        assert _조각(_엔진(tmp_path, base=base)) == 기대
    assert not [r for r in caplog.records if "langfuse_hook" in r.getMessage()]


def test_켜짐과_파일_있음이면_Stop과_SessionEnd에_한_번씩(tmp_path: Path) -> None:
    훅 = _훅_파일(tmp_path)
    조각 = _조각(_엔진(tmp_path, str(훅)))
    assert _훅_명령(조각, "Stop").count(str(훅)) == 1
    assert _훅_명령(조각, "SessionEnd") == [str(훅)]


def test_켜짐인데_파일이_없으면_훅이_없고_경고는_한_번(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    없는경로 = str(tmp_path / "없음.sh")
    engine = _엔진(tmp_path, 없는경로)
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            조각 = _조각(engine)
    assert 없는경로 not in json.dumps(조각)
    경고 = [r for r in caplog.records if "langfuse_hook" in r.getMessage()]
    assert len(경고) == 1


def test_운영_settings에_이미_있으면_다시_붙이지_않는다(tmp_path: Path) -> None:
    훅 = _훅_파일(tmp_path)
    항목 = [{"hooks": [{"type": "command", "command": str(훅), "timeout": 60}]}]
    base = {"hooks": {"Stop": 항목, "SessionEnd": 항목}}
    조각 = _조각(_엔진(tmp_path, str(훅), base))
    assert _훅_명령(조각, "Stop").count(str(훅)) == 1
    assert _훅_명령(조각, "SessionEnd").count(str(훅)) == 1


def test_물결표_경로는_홈으로_풀린다(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    훅 = _훅_파일(tmp_path)
    조각 = _조각(_엔진(tmp_path, "~/hook.sh"))
    assert _훅_명령(조각, "Stop").count(str(훅)) == 1
