"""소유자 턴을 MCP 서버 env 로 주입하는 공통 골격을 본다 (H19/H20 소유자 축).

마메치는 렌더 MCP 서버(mermaid)에 MAMETCHI_POST_ANY 를 소유자(김태일) 턴에만 "1"
로 넣어, 채널별 렌더 가드를 소유자 턴에서만 풀었다(bot.py:3225
`env["MAMETCHI_POST_ANY"] = "1" if is_owner else ""`, 서버는 mermaid_render.py:224
에서 `== "1"` 일 때만 다른 채널 게시를 허용). 앞선 채널 축(turn_env `channel`)은
"이 턴이 어느 채널인지"만 넘겨 가드가 늘 현재 채널로 제한됐고, 소유자 턴에서 가드를
푸는 반쪽이 없었다 — 그 공통 골격을 turn_env 의 `is_owner` 게이트 키로 채운다.

`is_owner` 는 `can_act` 와 같은 불리언 게이트다: 소유자 턴이면 변수를 "1" 로 넣고,
아니면 아예 뺀다(서버가 미설정을 기본값으로 읽어 가드가 선다). 소유자 여부는 엔진이
이미 아는 사실(EngineRequest.trust_level is OWNER)이라 채널 토큰처럼 불투명하게
흘릴 필요가 없다 — can_act 처럼 엔진이 요청에서 직접 끌어낸다. env 변수 이름
(POST_ANY)과 서버(mermaid)는 조직 설정, "소유자 턴에만 푼다"가 공통 골격이다.
세 엔진이 같은 프로필을 같게 다루는지 함께 본다(sca-wdzh).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from slack_cli_agent.auth.principal import TrustLevel
from slack_cli_agent.config.profile import McpServerSpec, Profile
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.errors import ConfigError
from slack_cli_agent.engine.base import Engine, EngineRequest
from slack_cli_agent.engine.claude import ClaudeEngine
from slack_cli_agent.engine.codex import CodexEngine
from slack_cli_agent.engine.gemini import GeminiEngine
from slack_cli_agent.engine.tool_selection import ToolSelection

_ENGINES: dict[str, Callable[[Profile, RuntimeSettings], Engine]] = {
    "claude": ClaudeEngine,
    "codex": CodexEngine,
    "gemini": GeminiEngine,
}

_소유자변수 = "POST_ANY"


class TestOwnerEnvSpec:
    def _spec(self) -> McpServerSpec:
        return McpServerSpec.from_dict(
            "mermaid", {"command": "머메이드", "turn_env": {_소유자변수: "is_owner"}}
        )

    def test_소유자_턴이면_변수를_1로_넣는다(self) -> None:
        assert self._spec().turn_env_for(can_act=False, is_owner=True) == {
            _소유자변수: "1"
        }

    def test_비소유자_턴이면_변수를_뺀다(self) -> None:
        # 빈 값이 아니라 아예 빼서 서버가 미설정(가드 작동)을 기본으로 읽게 한다.
        assert self._spec().turn_env_for(can_act=True, is_owner=False) == {}

    def test_기본값은_비소유자다(self) -> None:
        assert self._spec().turn_env_for(can_act=True) == {}

    def test_소유자_게이트는_쓰기여부와_독립이다(self) -> None:
        # 읽기 턴(can_act=False)이라도 소유자면 가드를 푼다.
        assert self._spec().turn_env_for(can_act=False, is_owner=True) == {
            _소유자변수: "1"
        }

    def test_모르는_맥락키는_로드에서_막는다(self) -> None:
        with pytest.raises(ConfigError):
            McpServerSpec.from_dict(
                "mermaid", {"command": "머메이드", "turn_env": {_소유자변수: "없는키"}}
            )

    def test_세_게이트와_채널을_한_서버에서_함께_쓴다(self) -> None:
        spec = McpServerSpec.from_dict(
            "모두",
            {
                "command": "서버",
                "turn_env": {
                    "WRITE": "can_act",
                    "ANY": "is_owner",
                    "CH": "channel",
                },
            },
        )
        # 소유자 행동 턴: 세 축이 모두 열린다.
        assert spec.turn_env_for(
            can_act=True, is_owner=True, turn_context={"channel": "C1"}
        ) == {"WRITE": "1", "ANY": "1", "CH": "C1"}
        # 비소유자 읽기 턴: 게이트 둘은 빠지고 채널만 남는다.
        assert spec.turn_env_for(
            can_act=False, is_owner=False, turn_context={"channel": "C1"}
        ) == {"CH": "C1"}


def _profile(engine_type: str) -> Profile:
    return Profile.from_dict({
        "name": "봇",
        "primary_engine": {"type": engine_type, "binary": f"/usr/bin/{engine_type}", "model": "m"},
        "owner_user_id": "U1",
        "troubleshoot_channel": "C1",
        "mcp_servers": {"mermaid": {"command": "머메이드", "turn_env": {_소유자변수: "is_owner"}}},
    })


def _command(engine_type: str, trust: TrustLevel, workdir: Path) -> str:
    engine = _ENGINES[engine_type](_profile(engine_type), RuntimeSettings())
    request = EngineRequest(
        prompt="프롬프트", system_prompt="지침",
        session_id="11111111-1111-1111-1111-111111111111", resume=False,
        model="m", effort="medium", workdir=workdir,
        tools=ToolSelection.allow(["Read", "Grep"]),
        trust_level=trust,
    )
    engine.prepare(request)
    본문 = " ".join(engine.build_command(request))
    # agy writes its servers to a file in prepare() rather than the command.
    config = workdir / ".agents" / "mcp_config.json"
    if config.is_file():
        본문 += " " + config.read_text(encoding="utf-8")
    return 본문


@pytest.mark.parametrize("engine_type", sorted(_ENGINES))
class TestOwnerEnvWiring:
    def test_소유자_턴이면_명령에_소유자변수가_실린다(
        self, engine_type: str, tmp_path: Path
    ) -> None:
        본문 = _command(engine_type, TrustLevel.OWNER, tmp_path)
        assert _소유자변수 in 본문

    def test_비소유자_턴이면_명령에_소유자변수가_없다(
        self, engine_type: str, tmp_path: Path
    ) -> None:
        본문 = _command(engine_type, TrustLevel.GENERAL, tmp_path)
        assert _소유자변수 not in 본문
