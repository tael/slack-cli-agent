"""원격 MCP 서버의 env 를 세 엔진이 같게 다루는지 본다 (sca-wdzh).

env 는 stdio 서버로 띄우는 프로세스의 환경이다. 원격 서버에는 띄울 프로세스가
없어 claude 의 원격 항목(type/url/headers)에도 agy 의 serverUrl 항목에도 그
자리가 없다. 코덱스만 원격에도 실어 같은 프로필이 엔진마다 다르게 돌았다.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from slack_cli_agent.config.profile import Profile
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.engine.base import Engine, EngineRequest
from slack_cli_agent.engine.claude import ClaudeEngine
from slack_cli_agent.engine.codex import CodexEngine
from slack_cli_agent.engine.gemini import GeminiEngine

_ENGINES: dict[str, Callable[[Profile, RuntimeSettings], Engine]] = {
    "claude": ClaudeEngine,
    "codex": CodexEngine,
    "gemini": GeminiEngine,
}

_키 = "원격환경키"
_값 = "원격환경값"


def _profile(engine_type: str, server: dict[str, object]) -> Profile:
    return Profile.from_dict({
        "name": "봇",
        "primary_engine": {"type": engine_type, "binary": f"/usr/bin/{engine_type}", "model": "m"},
        "owner_user_id": "U1",
        "troubleshoot_channel": "C1",
        "mcp_servers": {"서버": server},
    })


def _주입된_설정(engine_type: str, profile: Profile, workdir: Path) -> str:
    engine = _ENGINES[engine_type](profile, RuntimeSettings())
    request = EngineRequest(
        prompt="프롬프트", system_prompt="지침",
        session_id="11111111-1111-1111-1111-111111111111", resume=False,
        model="m", effort="medium", workdir=workdir,
    )
    engine.prepare(request)
    본문 = " ".join(engine.build_command(request))
    config = workdir / ".agents" / "mcp_config.json"
    if config.is_file():
        본문 += " " + config.read_text(encoding="utf-8")
    return 본문


@pytest.mark.parametrize("engine_type", sorted(_ENGINES))
class TestMCP원격env:
    def test_원격_서버의_env_는_어느_엔진에도_안_실린다(
        self, engine_type: str, tmp_path: Path
    ) -> None:
        본문 = _주입된_설정(
            engine_type,
            _profile(engine_type, {"url": "https://mcp.example.com/api", "env": {_키: _값}}),
            tmp_path,
        )
        assert _키 not in 본문
        assert _값 not in 본문

    def test_stdio_서버의_env_는_세_엔진_모두_싣는다(
        self, engine_type: str, tmp_path: Path
    ) -> None:
        본문 = _주입된_설정(
            engine_type, _profile(engine_type, {"command": "서버", "env": {_키: _값}}), tmp_path
        )
        assert _키 in 본문
        assert _값 in 본문
