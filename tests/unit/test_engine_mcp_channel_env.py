"""턴의 채널을 MCP 서버 env 로 주입하는 공통 골격을 본다 (H19/H20).

마메치는 렌더 MCP 서버(mermaid)에 MAMETCHI_POST_CHANNEL 을 턴별로 넣어, 서버가
"이 턴이 어느 채널에 올리는지"를 알고 채널별 렌더 가드를 걸게 했다(run_claude 영역).
이 엔진의 turn_env 는 can_act(쓰기 게이트) 한 축만 알아, 채널 토큰을 서버로 넘길
경로가 없었다 — 그 공통 골격을 McpServerSpec.turn_env 의 `channel` 키 +
EngineRequest.turn_context 로 채운다.

env 변수 이름(POST_CHANNEL)과 서버(mermaid)는 조직 설정이고, "이 턴의 채널을
넘긴다"는 배선이 공통이다. 채널 토큰은 엔진 층에 불투명하게(opaque) 흐른다 —
request_id 와 같은 취급이라 슬랙 식별자가 엔진 로직에 새지 않는다(sca-4ol).
세 엔진이 같은 프로필을 같게 다루는지 함께 본다(sca-wdzh).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from slack_cli_agent.config.profile import McpServerSpec, Profile
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.errors import ConfigError
from slack_cli_agent.engine.base import Engine, EngineRequest
from slack_cli_agent.engine.claude import ClaudeEngine
from slack_cli_agent.engine.codex import CodexEngine
from slack_cli_agent.engine.gemini import GeminiEngine
from slack_cli_agent.engine.tool_selection import ToolAccess, ToolSelection

_ENGINES: dict[str, Callable[[Profile, RuntimeSettings], Engine]] = {
    "claude": ClaudeEngine,
    "codex": CodexEngine,
    "gemini": GeminiEngine,
}

_채널변수 = "POST_CHANNEL"
_채널 = "C01EWFYL077"


class TestChannelEnvSpec:
    def _spec(self) -> McpServerSpec:
        return McpServerSpec.from_dict(
            "mermaid", {"command": "머메이드", "turn_env": {_채널변수: "channel"}}
        )

    def test_채널_토큰이_있으면_그_값을_넣는다(self) -> None:
        assert self._spec().turn_env_for(
            can_act=False, turn_context={"channel": _채널}
        ) == {_채널변수: _채널}

    def test_읽기_턴에도_채널은_주입된다(self) -> None:
        # 채널 가드는 쓰기 여부와 무관하다 — 읽기 턴에도 어느 채널인지 알아야 한다.
        assert self._spec().turn_env_for(
            can_act=True, turn_context={"channel": _채널}
        ) == {_채널변수: _채널}

    def test_채널_토큰이_비면_변수를_뺀다(self) -> None:
        # 빈 값은 아예 빼서 서버가 미설정을 기본값으로 읽게 한다.
        assert self._spec().turn_env_for(can_act=True, turn_context={"channel": ""}) == {}

    def test_맥락이_없으면_변수를_뺀다(self) -> None:
        assert self._spec().turn_env_for(can_act=True) == {}

    def test_모르는_맥락키는_로드에서_막는다(self) -> None:
        with pytest.raises(ConfigError):
            McpServerSpec.from_dict(
                "mermaid", {"command": "머메이드", "turn_env": {_채널변수: "없는키"}}
            )

    def test_can_act_와_channel_을_한_서버에서_함께_쓴다(self) -> None:
        spec = McpServerSpec.from_dict(
            "둘다",
            {"command": "서버", "turn_env": {"WRITE": "can_act", _채널변수: "channel"}},
        )
        assert spec.turn_env_for(can_act=True, turn_context={"channel": _채널}) == {
            "WRITE": "1",
            _채널변수: _채널,
        }
        # 읽기 턴: 쓰기 게이트는 빠지고 채널만 남는다.
        assert spec.turn_env_for(can_act=False, turn_context={"channel": _채널}) == {
            _채널변수: _채널,
        }


def _profile(engine_type: str) -> Profile:
    return Profile.from_dict({
        "name": "봇",
        "primary_engine": {"type": engine_type, "binary": f"/usr/bin/{engine_type}", "model": "m"},
        "owner_user_id": "U1",
        "troubleshoot_channel": "C1",
        "mcp_servers": {"mermaid": {"command": "머메이드", "turn_env": {_채널변수: "channel"}}},
    })


def _command(engine_type: str, turn_context: dict[str, str], workdir: Path) -> str:
    engine = _ENGINES[engine_type](_profile(engine_type), RuntimeSettings())
    request = EngineRequest(
        prompt="프롬프트", system_prompt="지침",
        session_id="11111111-1111-1111-1111-111111111111", resume=False,
        model="m", effort="medium", workdir=workdir,
        # 서버 이름이 허용목록에 있어야 claude 가 붙인다 (S2 1단계). 조회로
        # 묶어 두어 읽기 턴 성격은 그대로다.
        tools=ToolSelection(
            access=ToolAccess.ALLOWLIST, names=("Read", "Grep", "mcp__mermaid__render"),
            lookups=frozenset({"mcp__mermaid__render"}),
        ),
        turn_context=turn_context,
    )
    engine.prepare(request)
    본문 = " ".join(engine.build_command(request))
    # agy writes its servers to a file in prepare() rather than the command.
    config = workdir / ".agents" / "mcp_config.json"
    if config.is_file():
        본문 += " " + config.read_text(encoding="utf-8")
    return 본문


@pytest.mark.parametrize("engine_type", sorted(_ENGINES))
class TestChannelEnvWiring:
    def test_채널이_있으면_명령에_채널토큰이_실린다(
        self, engine_type: str, tmp_path: Path
    ) -> None:
        본문 = _command(engine_type, {"channel": _채널}, tmp_path)
        assert _채널변수 in 본문
        assert _채널 in 본문

    def test_맥락이_비면_채널변수가_빠진다(
        self, engine_type: str, tmp_path: Path
    ) -> None:
        본문 = _command(engine_type, {}, tmp_path)
        assert _채널변수 not in 본문
