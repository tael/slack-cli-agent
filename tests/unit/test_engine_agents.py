"""프로필 agents 키와 claude --agents 인자 (W3).

실측 근거는 claude 2.1.321, 2026-11-10 : 작업자 도구는 정의 tools 와 턴
--tools 의 교집합이고, Agent 가 없는 턴에서는 정의를 넘겨도 부를 수 없다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from slack_cli_agent.config.profile import AgentDefinition, Profile
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.errors import ConfigError
from slack_cli_agent.engine.base import EngineRequest
from slack_cli_agent.engine.claude import ClaudeEngine
from slack_cli_agent.engine.codex import CodexEngine
from slack_cli_agent.engine.tool_selection import ToolSelection
from slack_cli_agent.guard.finish_review import ToolCall, _is_advisor_call

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "profiles" / "example.example.json"

LOOKUP = {
    "description": "조회 전용 작업자",
    "model": "haiku",
    "tools": ["Read", "Grep", "Glob", "mcp__probe__echo"],
    "prompt": "조회만 한다",
}


def _profile(engine_type: str = "claude", **extra: Any) -> Profile:
    return Profile.from_dict({
        "name": "bot",
        "primary_engine": {"type": engine_type, "binary": f"/usr/bin/{engine_type}", "model": "m"},
        "owner_user_id": "U-OWNER",
        "troubleshoot_channel": "C-T",
        **extra,
    })


def _request(tmp_path: Path, tools: ToolSelection) -> EngineRequest:
    return EngineRequest(
        prompt="p",
        system_prompt="s",
        session_id="11111111-1111-1111-1111-111111111111",
        resume=False,
        model="m",
        effort="medium",
        workdir=tmp_path,
        tools=tools,
    )


def _agents_arg(cmd: list[str]) -> dict[str, Any] | None:
    if "--agents" not in cmd:
        return None
    return json.loads(cmd[cmd.index("--agents") + 1])


OWNER_TOOLS = ToolSelection.allow(["Read", "Grep", "Glob", "Bash", "Agent", "mcp__probe__*"])
BASE_TOOLS = ToolSelection.allow(["Read", "Grep", "Glob"])


class Test프로필_파싱:
    def test_키가_없으면_빈_사전이다(self) -> None:
        assert dict(_profile().agents) == {}

    def test_인라인_정의를_읽는다(self) -> None:
        agents = _profile(agents={"lookup": LOOKUP}).agents
        d = agents["lookup"]
        assert (d.name, d.model, d.tools, d.prompt) == (
            "lookup", "haiku", ("Read", "Grep", "Glob", "mcp__probe__echo"), "조회만 한다"
        )

    def test_tools_를_생략하면_None_이다(self) -> None:
        d = AgentDefinition.from_dict("x", {"description": "d", "prompt": "p"})
        assert d.tools is None
        assert "tools" not in d.cli_entry()
        assert "model" not in d.cli_entry()

    @pytest.mark.parametrize(
        "name, data, needle",
        [
            ("Lookup", LOOKUP, "이름"),
            ("lookup", {**LOOKUP, "description": ""}, "description"),
            ("lookup", {"description": "d"}, "prompt"),
            ("lookup", {"description": "d", "prompt": "p", "prompt_file": "a.md"}, "prompt"),
            ("lookup", {**LOOKUP, "tools": []}, "tools"),
            ("lookup", {**LOOKUP, "tools": "Read"}, "tools"),
            ("lookup", {**LOOKUP, "tools": ["Read", ""]}, "tools"),
            ("lookup", {**LOOKUP, "model": 3}, "model"),
            ("lookup", "문자열", "객체"),
        ],
    )
    def test_잘못된_정의는_거부한다(self, name: str, data: Any, needle: str) -> None:
        with pytest.raises(ConfigError, match=needle):
            _profile(agents={name: data})

    def test_agents_가_객체가_아니면_거부한다(self) -> None:
        with pytest.raises(ConfigError, match="agents"):
            _profile(agents=["lookup"])

    def test_상대_prompt_file_은_프로필_파일_기준으로_읽는다(self, tmp_path: Path) -> None:
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "lookup.md").write_text("파일 지시문\n", encoding="utf-8")
        data = {
            "name": "bot",
            "primary_engine": {"type": "claude", "binary": "/usr/bin/claude", "model": "m"},
            "agents": {"lookup": {"description": "d", "prompt_file": "agents/lookup.md"}},
        }
        (tmp_path / "bot.json").write_text(json.dumps(data), encoding="utf-8")
        d = Profile.load("bot", [tmp_path]).agents["lookup"]
        assert d.prompt == "파일 지시문"
        assert d.prompt_file == tmp_path / "agents" / "lookup.md"

    def test_prompt_file_이_없으면_파일_이름을_적어_거부한다(self, tmp_path: Path) -> None:
        data = {
            "name": "bot",
            "primary_engine": {"type": "claude", "binary": "/usr/bin/claude", "model": "m"},
            "agents": {"lookup": {"description": "d", "prompt_file": "agents/none.md"}},
        }
        (tmp_path / "bot.json").write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ConfigError, match=r"bot\.json"):
            Profile.load("bot", [tmp_path])

    def test_절대_prompt_file_은_바로_읽는다(self, tmp_path: Path) -> None:
        f = tmp_path / "v.md"
        f.write_text("검증", encoding="utf-8")
        d = _profile(agents={"v": {"description": "d", "prompt_file": str(f)}}).agents["v"]
        assert d.prompt == "검증"

    def test_읽지_않은_상대_파일은_인자로_만들지_않는다(self) -> None:
        d = _profile(agents={"v": {"description": "d", "prompt_file": "agents/v.md"}}).agents["v"]
        with pytest.raises(ConfigError, match="읽히지"):
            d.cli_entry()


class Test인자_생성:
    def test_Agent_가_열린_턴에만_넘긴다(self, tmp_path: Path) -> None:
        engine = ClaudeEngine(_profile(agents={"lookup": LOOKUP}), RuntimeSettings())
        cmd = engine.build_command(_request(tmp_path, OWNER_TOOLS))
        assert _agents_arg(cmd) == {
            "lookup": {
                "description": "조회 전용 작업자",
                "prompt": "조회만 한다",
                "model": "haiku",
                "tools": ["Read", "Grep", "Glob", "mcp__probe__echo"],
            }
        }

    def test_예전_이름_Task_도_Agent_로_본다(self, tmp_path: Path) -> None:
        engine = ClaudeEngine(_profile(agents={"lookup": LOOKUP}), RuntimeSettings())
        cmd = engine.build_command(_request(tmp_path, ToolSelection.allow(["Read", "Task"])))
        assert _agents_arg(cmd) is not None

    @pytest.mark.parametrize(
        "tools",
        [BASE_TOOLS, ToolSelection.unrestricted(), ToolSelection.forbid_all()],
        ids=["base_tools", "unrestricted", "forbidden"],
    )
    def test_Agent_가_없는_턴에는_넘기지_않는다(self, tmp_path: Path, tools: ToolSelection) -> None:
        engine = ClaudeEngine(_profile(agents={"lookup": LOOKUP}), RuntimeSettings())
        assert "--agents" not in engine.build_command(_request(tmp_path, tools))

    def test_agents_가_없는_프로필은_명령이_그대로다(self, tmp_path: Path) -> None:
        """키를 더한 뒤에도 정의 없는 프로필의 명령은 한 글자도 바뀌지 않는다."""
        plain = ClaudeEngine(_profile(), RuntimeSettings())
        with_agents = ClaudeEngine(_profile(agents={"lookup": LOOKUP}), RuntimeSettings())
        req = _request(tmp_path, OWNER_TOOLS)
        cmd = plain.build_command(req)
        assert "--agents" not in cmd
        other = with_agents.build_command(req)
        i = other.index("--agents")
        assert other[:i] + other[i + 2:] == cmd

    def test_도구_축_판정은_agents_와_무관하다(self, tmp_path: Path) -> None:
        plain = ClaudeEngine(_profile(), RuntimeSettings())
        with_agents = ClaudeEngine(_profile(agents={"lookup": LOOKUP}), RuntimeSettings())
        req = _request(tmp_path, OWNER_TOOLS)
        assert plain.capabilities_for(req) == with_agents.capabilities_for(req)

    def test_codex_엔진은_agents_를_쓰지_않는다(self, tmp_path: Path) -> None:
        engine = CodexEngine(_profile("codex", agents={"lookup": LOOKUP}), RuntimeSettings())
        cmd = " ".join(engine.build_command(_request(tmp_path, OWNER_TOOLS)))
        assert "--agents" not in cmd
        assert "조회만 한다" not in cmd


class Test배포_정의:
    def test_견본_정의_세_개가_파일에서_읽힌다(self) -> None:
        profile = Profile._read(EXAMPLE)
        assert set(profile.agents) == {"lookup", "analyst", "verifier"}
        for d in profile.agents.values():
            assert d.prompt and d.tools
            # 정의당 2k토큰 이하. 한국어는 글자당 1토큰 남짓이라 글자 수로 상한을 둔다
            assert len(d.prompt) < 1500, (d.name, len(d.prompt))
            assert "15회" in d.prompt and "5분" in d.prompt
            for label in ("결론", "근거 수치", "실패한 조회"):
                assert label in d.prompt
            assert "Agent" not in d.tools and "Task" not in d.tools

    def test_조회_전용_정의에는_Bash_와_쓰기_도구가_없다(self) -> None:
        """정의 tools 에 Bash(git:*) 처럼 범위를 적어도 Bash 전체가 열린다
        (2026-11-10 실측 G). 조회 전용 정의는 Bash 를 아예 빼야 한다."""
        agents = Profile._read(EXAMPLE).agents
        for name in ("lookup", "verifier"):
            tools = agents[name].tools or ()
            assert not any(t.split("(")[0] in {"Bash", "Write", "Edit", "NotebookEdit"} for t in tools)
        for d in agents.values():
            assert not any(t.endswith("__*") for t in d.tools or ()), d.name

    def test_모델은_조언자가_아니다(self) -> None:
        for d in Profile._read(EXAMPLE).agents.values():
            assert "fable" not in d.model

    def test_작업자_호출은_조언자_호출로_세지_않는다(self) -> None:
        call = ToolCall(name="Agent", input={"subagent_type": "lookup", "prompt": "p"})
        assert _is_advisor_call(call, "fable") is False
