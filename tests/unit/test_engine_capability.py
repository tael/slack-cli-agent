"""엔진이 실제로 무엇을 보장하는지 선언한다(sca-dyb.15 커밋 1단계).

호출자는 도구 제한이 걸린 줄 아는데 강제 수준이 엔진마다 다르다. claude 는
--allowedTools 로 정확한 허용목록, codex 는 sandbox 모드, agy 는
--dangerously-skip-permissions 로 아무 제한이 없다. 이 단계는 그 차이를 값으로
드러내기만 한다 — 실행 동작은 바꾸지 않는다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_engine import claude_profile, gemini_profile, profile_with, request

from slack_cli_agent.config.profile import Profile
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.engine.base import Engine
from slack_cli_agent.engine.capability import (
    EngineCapabilities,
    ExecutionIsolation,
    ExecutionRequirements,
    InstructionBoundary,
    ToolRestriction,
)
from slack_cli_agent.engine.claude import ClaudeEngine
from slack_cli_agent.engine.codex import CodexEngine
from slack_cli_agent.engine.gemini import GeminiEngine
from slack_cli_agent.engine.runner import CAPABILITY_KIND, EngineRunner, FallbackEngine
from slack_cli_agent.engine.switcher import EngineSwitcher
from slack_cli_agent.observability.audit import INCIDENT_KINDS, IncidentKind

SETTINGS = RuntimeSettings()


def codex_profile(tmp_path: Path, **options: Any) -> Profile:
    primary: dict[str, Any] = {"type": "codex", "binary": "codex", "model": "gpt-5"}
    if options:
        primary["options"] = options
    return profile_with(primary, tmp_path=tmp_path)


class Test축은_따로_본다:
    def test_세_축이_각각_독립으로_선언된다(self) -> None:
        """단일 등급으로 합치면 도구 제한과 파일계 경계가 섞인다."""
        보장 = EngineCapabilities(
            tool_restriction=ToolRestriction.EXACT_ALLOWLIST,
            execution_isolation=ExecutionIsolation.NONE,
            instruction_boundary=InstructionBoundary.NATIVE,
        )
        assert 보장.tool_restriction is ToolRestriction.EXACT_ALLOWLIST
        assert 보장.execution_isolation is ExecutionIsolation.NONE


class Test요구와_대조:
    def _요구(self, **kw: Any) -> ExecutionRequirements:
        return ExecutionRequirements(**kw)

    def test_요구가_없으면_어느_엔진도_통과한다(self) -> None:
        아무것도 = EngineCapabilities(
            ToolRestriction.NONE, ExecutionIsolation.NONE, InstructionBoundary.UNAVAILABLE
        )
        assert self._요구().unmet(아무것도) == ()

    def test_요구보다_약하면_그_축을_돌려준다(self) -> None:
        실제 = EngineCapabilities(
            ToolRestriction.NONE, ExecutionIsolation.NONE, InstructionBoundary.UNAVAILABLE
        )
        요구 = self._요구(tool_restriction=ToolRestriction.EXACT_ALLOWLIST)
        assert 요구.unmet(실제) == ("tool_restriction",)

    def test_요구보다_강하면_통과한다(self) -> None:
        """허용목록은 샌드박스보다 강하다. 더 강한 것을 막으면 안 된다."""
        실제 = EngineCapabilities(
            ToolRestriction.EXACT_ALLOWLIST, ExecutionIsolation.NONE, InstructionBoundary.NATIVE
        )
        요구 = self._요구(tool_restriction=ToolRestriction.COARSE_SANDBOX)
        assert 요구.unmet(실제) == ()

    def test_못_맞춘_축을_전부_돌려준다(self) -> None:
        실제 = EngineCapabilities(
            ToolRestriction.NONE, ExecutionIsolation.NONE, InstructionBoundary.UNAVAILABLE
        )
        요구 = self._요구(
            tool_restriction=ToolRestriction.EXACT_ALLOWLIST,
            execution_isolation=ExecutionIsolation.READONLY_SANDBOX,
            instruction_boundary=InstructionBoundary.NATIVE,
        )
        assert 요구.unmet(실제) == (
            "tool_restriction",
            "execution_isolation",
            "instruction_boundary",
        )

    def test_완화_허용은_기본이_꺼져_있다(self) -> None:
        """기본이 켜져 있으면 아무도 안 끄고 강제가 없는 것과 같아진다."""
        assert self._요구().allow_audited_downgrade is False


class Test엔진별_선언:
    def test_claude_는_허용목록을_정확히_강제한다(self, tmp_path: Path) -> None:
        엔진 = ClaudeEngine(claude_profile(tmp_path), SETTINGS)
        보장 = 엔진.capabilities_for(request(allowed_tools=("Read", "Grep")))
        assert 보장.tool_restriction is ToolRestriction.EXACT_ALLOWLIST
        assert 보장.instruction_boundary is InstructionBoundary.NATIVE

    def test_claude_도_허용목록이_비면_도구_제한이_없다(self, tmp_path: Path) -> None:
        """--allowedTools 를 빈 문자열로 넘긴 것은 제한을 건 것이 아니다."""
        엔진 = ClaudeEngine(claude_profile(tmp_path), SETTINGS)
        보장 = 엔진.capabilities_for(request(allowed_tools=()))
        assert 보장.tool_restriction is ToolRestriction.NONE

    def test_claude_는_파일계_격리가_없다(self, tmp_path: Path) -> None:
        """읽기 전용은 도구 목록으로 지키는 것이지 샌드박스가 아니다."""
        엔진 = ClaudeEngine(claude_profile(tmp_path), SETTINGS)
        assert 엔진.capabilities_for(request()).execution_isolation is ExecutionIsolation.NONE

    def test_agy_는_도구도_격리도_보장하지_않는다(self, tmp_path: Path) -> None:
        """신뢰 경계만 표시 수준이다 - 한 -p 문자열 안에 표식을 넣을 뿐이라
        강제가 아니다(sca-dyb.12)."""
        엔진 = GeminiEngine(gemini_profile(tmp_path), SETTINGS)
        보장 = 엔진.capabilities_for(request(model="gemini-3.8-flash"))
        assert 보장.tool_restriction is ToolRestriction.NONE
        assert 보장.execution_isolation is ExecutionIsolation.NONE
        assert 보장.instruction_boundary is InstructionBoundary.PROMPT_ONLY

    def test_codex_기본은_전체_접근이라_격리가_없다(self, tmp_path: Path) -> None:
        엔진 = CodexEngine(codex_profile(tmp_path), SETTINGS)
        보장 = 엔진.capabilities_for(request(model="gpt-5"))
        assert 보장.execution_isolation is ExecutionIsolation.NONE
        assert 보장.tool_restriction is ToolRestriction.NONE

    def test_codex_읽기전용_프로필은_격리를_보장한다(self, tmp_path: Path) -> None:
        """클래스 속성이 아니라 이번 요청의 실제 보장을 봐야 하는 이유다."""
        엔진 = CodexEngine(codex_profile(tmp_path, sandbox="read-only"), SETTINGS)
        보장 = 엔진.capabilities_for(request(model="gpt-5"))
        assert 보장.execution_isolation is ExecutionIsolation.READONLY_SANDBOX
        assert 보장.tool_restriction is ToolRestriction.COARSE_SANDBOX

    def test_codex_작업공간_쓰기는_읽기전용보다_약하다(self, tmp_path: Path) -> None:
        엔진 = CodexEngine(codex_profile(tmp_path, sandbox="workspace-write"), SETTINGS)
        보장 = 엔진.capabilities_for(request(model="gpt-5"))
        assert 보장.execution_isolation is ExecutionIsolation.WORKSPACE_WRITE
        요구 = ExecutionRequirements(execution_isolation=ExecutionIsolation.READONLY_SANDBOX)
        assert 요구.unmet(보장) == ("execution_isolation",)

    def test_codex_는_지침_경계가_네이티브다(self, tmp_path: Path) -> None:
        """developer_instructions 는 프롬프트와 다른 자리다."""
        엔진 = CodexEngine(codex_profile(tmp_path), SETTINGS)
        assert 엔진.capabilities_for(request(model="gpt-5")).instruction_boundary is (
            InstructionBoundary.NATIVE
        )


class Test선언_누락을_막는다:
    @pytest.mark.parametrize("engine_cls", [ClaudeEngine, CodexEngine, GeminiEngine])
    def test_모든_엔진이_보장을_선언한다(self, engine_cls: type[Engine]) -> None:
        """선언이 없으면 대조가 조용히 통과해 강제가 사라진다."""
        assert isinstance(engine_cls.capabilities, EngineCapabilities), engine_cls


# 2단계 — 요구와 실제 보장을 감사에 기록한다. 아직 차단하지 않는다.


class _감사:
    def __init__(self) -> None:
        self.기록: list[tuple[str, dict[str, Any]]] = []

    def record(self, kind: str, *, channel: str = "", thread_ts: str = "", **fields: Any) -> None:
        self.기록.append((kind, fields))


class Test보장을_감사에_남긴다:
    def _돌린다(self, tmp_path: Path, 감사: _감사, **요청: Any) -> None:
        from test_engine import FakeCompleted, RecordingEngine, 통과정책

        엔진 = RecordingEngine(claude_profile(tmp_path), SETTINGS)
        runner = EngineRunner(
            SETTINGS,
            subprocess_runner=lambda cmd, cwd, timeout, env=None: FakeCompleted(
                stdout="답변", returncode=0
            ),
            environment_policy=통과정책(),
            audit=감사,
        )
        runner.run(엔진, request(**요청))

    def test_모든_요청의_요구와_실제_보장을_남긴다(self, tmp_path: Path) -> None:
        감사 = _감사()
        self._돌린다(
            tmp_path,
            감사,
            requirements=ExecutionRequirements(tool_restriction=ToolRestriction.EXACT_ALLOWLIST),
        )
        [(kind, 필드)] = 감사.기록
        assert kind == CAPABILITY_KIND
        assert 필드["required"]["tool_restriction"] == "exact_allowlist"
        assert 필드["actual"]["tool_restriction"] == "none"
        assert 필드["engine"] == "fake"

    def test_못_맞춘_축을_함께_남긴다(self, tmp_path: Path) -> None:
        """기록만 보고 3단계에서 무엇이 막힐지 미리 셀 수 있어야 한다."""
        감사 = _감사()
        self._돌린다(
            tmp_path,
            감사,
            requirements=ExecutionRequirements(
                tool_restriction=ToolRestriction.EXACT_ALLOWLIST,
                instruction_boundary=InstructionBoundary.NATIVE,
            ),
        )
        assert 감사.기록[0][1]["unmet"] == ["tool_restriction", "instruction_boundary"]

    def test_요구가_없어도_실제_보장은_남는다(self, tmp_path: Path) -> None:
        """0건이 요구 없음인지 기록 자체가 안 도는 것인지 구분돼야 한다."""
        감사 = _감사()
        self._돌린다(tmp_path, 감사)
        [(_, 필드)] = 감사.기록
        assert 필드["required"] == {}
        assert 필드["unmet"] == []

    def test_아직_아무것도_막지_않는다(self, tmp_path: Path) -> None:
        from test_engine import FakeCompleted, RecordingEngine, 통과정책

        엔진 = RecordingEngine(claude_profile(tmp_path), SETTINGS)
        runner = EngineRunner(
            SETTINGS,
            subprocess_runner=lambda cmd, cwd, timeout, env=None: FakeCompleted(
                stdout="답변", returncode=0
            ),
            environment_policy=통과정책(),
            audit=_감사(),
        )
        응답 = runner.run(
            엔진,
            request(
                requirements=ExecutionRequirements(
                    tool_restriction=ToolRestriction.EXACT_ALLOWLIST
                )
            ),
        )
        assert 응답.ok is True

    def test_감사가_없어도_실행은_그대로다(self, tmp_path: Path) -> None:
        from test_engine import FakeCompleted, RecordingEngine, 통과정책

        엔진 = RecordingEngine(claude_profile(tmp_path), SETTINGS)
        runner = EngineRunner(
            SETTINGS,
            subprocess_runner=lambda cmd, cwd, timeout, env=None: FakeCompleted(
                stdout="답변", returncode=0
            ),
            environment_policy=통과정책(),
        )
        assert runner.run(엔진, request()).ok is True


class Test폴백은_실제로_도는_엔진의_보장을_낸다:
    def _폴백(self, tmp_path: Path) -> FallbackEngine:
        from engine_support import named
        from test_engine import RecordingEngine, 통과정책

        프로필 = claude_profile(tmp_path)
        일차 = ClaudeEngine(프로필, SETTINGS)
        이차 = named(RecordingEngine, "gemini", capabilities=GeminiEngine.capabilities)(프로필, SETTINGS)
        runner = EngineRunner(SETTINGS, environment_policy=통과정책())
        return FallbackEngine(일차, 이차, EngineSwitcher(tmp_path / "engine_state.json"), runner)

    def test_이차로_넘어가면_이차의_보장을_돌려준다(self, tmp_path: Path) -> None:
        """대리가 자기 기본값을 돌려주면 폴백 턴의 기록이 전부 거짓이 된다."""
        폴백 = self._폴백(tmp_path)
        assert 폴백.capabilities_for(request()) == ClaudeEngine.capabilities
        폴백._active = 폴백.secondary
        assert 폴백.capabilities_for(request()) == GeminiEngine.capabilities


class Test감사_종류:
    def test_보장_기록은_사고로_세지_않는다(self) -> None:
        """기준 통행량이라 사고 집계에 들어가면 사고율이 전부 바뀐다."""
        assert IncidentKind.CAPABILITY not in INCIDENT_KINDS
