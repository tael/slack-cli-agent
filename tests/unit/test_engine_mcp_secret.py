"""MCP 자격을 프로필 밖에서 주입하는 경로 (sca-dn4).

세 엔진이 같은 방식으로 다뤄져야 한다. 한 엔진만 해석하면 그 봇의 엔진을
바꾸는 순간 표기가 그대로 서버에 넘어가 인증만 실패한다.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from slack_cli_agent.config.profile import Profile
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.errors import ConfigError
from slack_cli_agent.engine.base import Engine, EngineRequest
from slack_cli_agent.engine.claude import ClaudeEngine
from slack_cli_agent.engine.codex import CodexEngine
from slack_cli_agent.engine.gemini import GeminiEngine

#: 값 표기가 type[Engine] 이면 Engine 이 추상이라 호출이 막힌다. 실제로 쓰는 것은
#: 생성자 호출이므로 그 서명으로 적는다.
_ENGINES: dict[str, Callable[[Profile, RuntimeSettings], Engine]] = {
    "claude": ClaudeEngine,
    "codex": CodexEngine,
    "gemini": GeminiEngine,
}


def _profile(engine_type: str, env_value: str) -> Profile:
    return Profile.from_dict({
        "name": "봇",
        "primary_engine": {"type": engine_type, "binary": f"/usr/bin/{engine_type}", "model": "m"},
        "owner_user_id": "U1",
        "troubleshoot_channel": "C1",
        "mcp_servers": {"깃허브": {"command": "서버", "env": {"GITHUB_TOKEN": env_value}}},
    })


def _request(workdir: Path) -> EngineRequest:
    return EngineRequest(
        prompt="프롬프트", system_prompt="지침",
        session_id="11111111-1111-1111-1111-111111111111", resume=False,
        model="m", effort="medium", workdir=workdir,
    )


def _주입된_설정(engine_type: str, profile: Profile, workdir: Path) -> str:
    engine = _ENGINES[engine_type](profile, RuntimeSettings())
    request = _request(workdir)
    engine.prepare(request)
    cmd = " ".join(engine.build_command(request))
    config = workdir / ".agents" / "mcp_config.json"
    if config.is_file():
        cmd += " " + config.read_text(encoding="utf-8")
    return cmd


@pytest.mark.parametrize("engine_type", sorted(_ENGINES))
class TestMCP자격주입:
    def test_환경변수_표기가_실제_값으로_바뀐다(
        self, engine_type: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GH", "진짜값")
        본문 = _주입된_설정(engine_type, _profile(engine_type, "${env:GH}"), tmp_path)
        assert "진짜값" in 본문
        assert "${env:GH}" not in 본문

    def test_파일_표기가_내용으로_바뀐다(
        self, engine_type: str, tmp_path: Path
    ) -> None:
        비밀 = tmp_path / "토큰"
        비밀.write_text("파일값\n", encoding="utf-8")
        본문 = _주입된_설정(engine_type, _profile(engine_type, f"${{file:{비밀}}}"), tmp_path)
        assert "파일값" in 본문

    def test_못_찾으면_빈_값을_넘기지_않고_실패한다(
        self, engine_type: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """빈 토큰을 넘기면 MCP 서버의 인증 실패로만 보여 원인이 안 보인다."""
        monkeypatch.delenv("GH", raising=False)
        with pytest.raises(ConfigError):
            _주입된_설정(engine_type, _profile(engine_type, "${env:GH}"), tmp_path)

    def test_평범한_값은_그대로_간다(self, engine_type: str, tmp_path: Path) -> None:
        본문 = _주입된_설정(engine_type, _profile(engine_type, "평문값"), tmp_path)
        assert "평문값" in 본문


def test_제미나이_설정_파일에_표기가_남지_않는다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """제미나이만 값이 디스크에 남는다. 거기에 표기가 남으면 해석이 안 된 것이다."""
    monkeypatch.setenv("GH", "진짜값")
    profile = _profile("gemini", "${env:GH}")
    engine = GeminiEngine(profile, RuntimeSettings())
    engine.prepare(_request(tmp_path))
    written = json.loads((tmp_path / ".agents" / "mcp_config.json").read_text(encoding="utf-8"))
    assert written["mcpServers"]["깃허브"]["env"] == {"GITHUB_TOKEN": "진짜값"}


def _원격프로필(engine_type: str, header_value: str) -> Profile:
    return Profile.from_dict({
        "name": "봇",
        "primary_engine": {"type": engine_type, "binary": f"/usr/bin/{engine_type}", "model": "m"},
        "owner_user_id": "U1",
        "troubleshoot_channel": "C1",
        "mcp_servers": {
            "원격": {"url": "https://mcp.example.com/api", "headers": {"Authorization": header_value}}
        },
    })


@pytest.mark.parametrize("engine_type", sorted(_ENGINES))
class TestMCP원격헤더:
    """원격 MCP 서버의 인증 헤더는 세 엔진 모두에서 실제로 넘어가야 한다.

    코덱스만 안 넘겨서 그 봇에서만 인증이 실패했다(sca-m7w). 프로필에는 적혀
    있으므로 설정만 보면 되는 줄 안다. 코덱스 설정 키는 config.toml 의
    mcp_servers.<이름>.http_headers 이고 codex-cli 0.154.0 의 `codex mcp list
    --json` 으로 확인했다.
    """

    def test_헤더가_실행_설정에_들어간다(self, engine_type: str, tmp_path: Path) -> None:
        본문 = _주입된_설정(engine_type, _원격프로필(engine_type, "Bearer 평문값"), tmp_path)
        assert "Authorization" in 본문
        assert "Bearer 평문값" in 본문

    def test_헤더의_표기도_실제_값으로_바뀐다(
        self, engine_type: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GH", "진짜토큰")
        본문 = _주입된_설정(engine_type, _원격프로필(engine_type, "${env:GH}"), tmp_path)
        assert "진짜토큰" in 본문
        assert "${env:GH}" not in 본문
