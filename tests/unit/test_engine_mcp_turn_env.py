"""턴 맥락에 따라 MCP 서버의 env 를 켜고 끄는 공통 골격을 본다 (H6/H7/H8).

마메치는 쓰기 MCP 서버(jira)의 WRITE_ENABLED 를 '행동 가능한 턴'에만 켜서,
읽기 전용 턴에서 쓰기 도구가 불려도 서버가 거부하게 했다(bot.py:2253 게이트).
이 엔진에는 턴별 동적 env 주입 경로가 없어 그 게이트에 도달할 수 없었다 —
그 공통 골격을 McpServerSpec.turn_env + ToolSelection.can_act 로 채운다.

env 변수 이름(WRITE_ENABLED)과 서버(jira)는 조직 설정이고, '행동 턴에만 켠다'는
판정이 공통이다. 세 엔진이 같은 프로필을 같게 다루는지 함께 본다(sca-wdzh).
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

_게이트 = "WRITE_ENABLED"


class TestCanAct:
    def test_금지된_턴은_행동하지_못한다(self) -> None:
        assert ToolSelection.forbid_all().can_act is False

    def test_무제한_턴은_행동한다(self) -> None:
        assert ToolSelection.unrestricted().can_act is True

    def test_읽기전용_도구만_있으면_행동_아니다(self) -> None:
        assert ToolSelection.allow(["Read", "Grep", "Glob"]).can_act is False

    def test_쓰기_도구가_하나라도_있으면_행동_턴(self) -> None:
        assert ToolSelection.allow(["Read", "Bash"]).can_act is True

    def test_mcp_쓰기도구도_행동_턴으로_센다(self) -> None:
        assert ToolSelection.allow(["Read", "mcp__jira__jira_add_comment"]).can_act is True


class TestTurnEnvSpec:
    def _spec(self) -> McpServerSpec:
        return McpServerSpec.from_dict(
            "jira", {"command": "지라", "turn_env": {_게이트: "can_act"}}
        )

    def test_행동_턴이면_게이트를_켠다(self) -> None:
        assert self._spec().turn_env_for(can_act=True) == {_게이트: "1"}

    def test_읽기_턴이면_게이트를_빼_버린다(self) -> None:
        # 비우지 않고 아예 뺀다 — 서버가 미설정을 '닫힘'으로 읽게.
        assert self._spec().turn_env_for(can_act=False) == {}

    def test_모르는_맥락키는_로드에서_막는다(self) -> None:
        with pytest.raises(ConfigError):
            McpServerSpec.from_dict("jira", {"command": "지라", "turn_env": {_게이트: "언젠가"}})

    def test_turn_env_없으면_빈_맵(self) -> None:
        spec = McpServerSpec.from_dict("jira", {"command": "지라"})
        assert spec.turn_env_for(can_act=True) == {}


def _profile(engine_type: str) -> Profile:
    return Profile.from_dict({
        "name": "봇",
        "primary_engine": {"type": engine_type, "binary": f"/usr/bin/{engine_type}", "model": "m"},
        "owner_user_id": "U1",
        "troubleshoot_channel": "C1",
        "mcp_servers": {"jira": {"command": "지라", "turn_env": {_게이트: "can_act"}}},
    })


def _command(engine_type: str, tools: ToolSelection, workdir: Path) -> str:
    engine = _ENGINES[engine_type](_profile(engine_type), RuntimeSettings())
    # 소유자 턴이다. codex 는 비소유자 턴을 read-only 로 내려 행동 게이트를 빼므로 (A9),
    # 게이트 배선을 보려면 행동할 수 있는 요청자로 둔다.
    request = EngineRequest(
        prompt="프롬프트", system_prompt="지침",
        session_id="11111111-1111-1111-1111-111111111111", resume=False,
        model="m", effort="medium", workdir=workdir, tools=tools,
        trust_level=TrustLevel.OWNER,
    )
    engine.prepare(request)
    본문 = " ".join(engine.build_command(request))
    # agy writes its servers to a file in prepare() rather than the command.
    config = workdir / ".agents" / "mcp_config.json"
    if config.is_file():
        본문 += " " + config.read_text(encoding="utf-8")
    return 본문


@pytest.mark.parametrize("engine_type", sorted(_ENGINES))
class TestTurnEnvWiring:
    def test_행동_턴이면_명령에_게이트가_실린다(
        self, engine_type: str, tmp_path: Path
    ) -> None:
        본문 = _command(engine_type, ToolSelection.allow(["Read", "Bash"]), tmp_path)
        assert _게이트 in 본문

    def test_읽기전용_턴이면_게이트가_빠진다(
        self, engine_type: str, tmp_path: Path
    ) -> None:
        본문 = _command(engine_type, ToolSelection.allow(["Read", "Grep"]), tmp_path)
        assert _게이트 not in 본문

    def test_금지된_턴이면_게이트가_빠진다(
        self, engine_type: str, tmp_path: Path
    ) -> None:
        본문 = _command(engine_type, ToolSelection.forbid_all(), tmp_path)
        assert _게이트 not in 본문
