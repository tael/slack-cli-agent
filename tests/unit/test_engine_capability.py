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
from test_engine import RecordingEngine, claude_profile, gemini_profile, profile_with, request

from slack_cli_agent.config.profile import Profile
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.engine.base import Engine
from slack_cli_agent.engine.capability import (
    TOOL_AXIS,
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

    def test_완화_대상_축은_기본이_비어_있다(self) -> None:
        """기본이 전부 완화면 아무도 안 끄고 강제가 없는 것과 같아진다."""
        assert self._요구().downgradable_axes == frozenset()

    def test_완화는_명시한_축에만_적용된다(self) -> None:
        """도구 축 설정 하나가 격리까지 낮추면 세 축을 따로 둔 뜻이 없다."""
        요구 = self._요구(
            tool_restriction=ToolRestriction.EXACT_ALLOWLIST,
            execution_isolation=ExecutionIsolation.READONLY_SANDBOX,
            downgradable_axes=frozenset({TOOL_AXIS}),
        )
        실제 = EngineCapabilities(
            ToolRestriction.NONE, ExecutionIsolation.NONE, InstructionBoundary.NATIVE
        )
        # 완화 대상이 아닌 축이 하나라도 걸리면 아무것도 완화하지 않는다.
        assert 요구.downgraded(요구.unmet(실제)) == ()

    def test_미충족이_전부_완화_대상이면_그_축들을_돌려준다(self) -> None:
        요구 = self._요구(
            tool_restriction=ToolRestriction.EXACT_ALLOWLIST,
            downgradable_axes=frozenset({TOOL_AXIS}),
        )
        실제 = EngineCapabilities(
            ToolRestriction.NONE, ExecutionIsolation.NONE, InstructionBoundary.NATIVE
        )
        assert 요구.downgraded(요구.unmet(실제)) == (TOOL_AXIS,)


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

    def test_어느_정책이_이_요구를_세웠는지_남는다(self, tmp_path: Path) -> None:
        """기록만 보고는 완화가 정책상 허용된 것인지 요구를 안 세운 것인지
        구분되지 않는다 (sca-98k 3/3)."""
        감사 = _감사()
        self._돌린다(
            tmp_path,
            감사,
            requirements=ExecutionRequirements(
                tool_restriction=ToolRestriction.EXACT_ALLOWLIST,
                downgradable_axes=frozenset({TOOL_AXIS}),
                policy="audited",
            ),
        )
        필드 = 감사.기록[0][1]
        assert 필드["policy"] == "audited"

    def test_완화가_실제로_적용된_경우를_구분해_남긴다(self, tmp_path: Path) -> None:
        """허용됐다는 것과 이번에 썼다는 것은 다르다. 완화 건수를 세려면
        후자가 필요하다."""
        감사 = _감사()
        self._돌린다(
            tmp_path,
            감사,
            requirements=ExecutionRequirements(
                tool_restriction=ToolRestriction.EXACT_ALLOWLIST,
                downgradable_axes=frozenset({TOOL_AXIS}),
                policy="audited",
            ),
        )
        assert 감사.기록[0][1]["downgrade_applied"] is True

    def test_보장을_맞춘_요청은_완화로_세지_않는다(self, tmp_path: Path) -> None:
        감사 = _감사()
        self._돌린다(
            tmp_path,
            감사,
            requirements=ExecutionRequirements(
                tool_restriction=ToolRestriction.NONE,
                downgradable_axes=frozenset({TOOL_AXIS}),
                policy="audited",
            ),
        )
        assert 감사.기록[0][1]["downgrade_applied"] is False

    def test_요구가_없어도_실제_보장은_남는다(self, tmp_path: Path) -> None:
        """0건이 요구 없음인지 기록 자체가 안 도는 것인지 구분돼야 한다."""
        감사 = _감사()
        self._돌린다(tmp_path, 감사)
        [(_, 필드)] = 감사.기록
        assert 필드["required"] == {}
        assert 필드["unmet"] == []

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


class _그만(Exception):
    """대역이 요청만 받고 멈춘다. 응답을 지어내면 계약이 바뀔 때 안 깨진다."""


class Test폴백은_요청의_요구를_그대로_넘긴다:
    """일차에서 세운 경계가 이차로 넘어갈 때 사라지면, 한도 소진이 그대로
    권한 우회가 된다 (sca-93u)."""

    def _폴백(self, tmp_path: Path) -> tuple[FallbackEngine, Any]:
        from engine_support import named
        from test_engine import RecordingEngine, profile_with, 통과정책

        프로필 = profile_with(
            {"type": "claude", "binary": "claude", "model": "claude-sonnet-5"},
            {"type": "gemini", "binary": "agy", "model": "gemini-3.8-flash"},
            tmp_path=tmp_path,
        )
        일차 = ClaudeEngine(프로필, SETTINGS)
        이차종류 = named(RecordingEngine, "gemini", capabilities=GeminiEngine.capabilities)
        이차 = 이차종류(프로필, SETTINGS)
        runner = EngineRunner(SETTINGS, environment_policy=통과정책())
        폴백 = FallbackEngine(일차, 이차, EngineSwitcher(tmp_path / "engine_state.json"), runner)
        return 폴백, 이차

    def test_이차로_넘길_때_요구를_버리지_않는다(self, tmp_path: Path) -> None:
        폴백, _ = self._폴백(tmp_path)
        요구 = ExecutionRequirements(tool_restriction=ToolRestriction.EXACT_ALLOWLIST)
        받은: list[Any] = []

        def 받아둔다(engine: Any, req: Any) -> Any:
            받은.append(req)
            raise _그만()

        폴백.runner.run = 받아둔다  # type: ignore[assignment,method-assign]
        with pytest.raises(_그만):
            폴백._run_secondary(request(requirements=요구))
        assert 받은[0].requirements == 요구

    def test_이차가_요구를_못_맞추면_실행하지_않는다(self, tmp_path: Path) -> None:
        """gemini 의 도구 제한은 NONE 이다. 정확한 허용 목록을 요구한 요청이
        폴백에서 그냥 도는 것이 이 이슈의 실제 피해다."""
        폴백, 이차 = self._폴백(tmp_path)
        요구 = ExecutionRequirements(tool_restriction=ToolRestriction.EXACT_ALLOWLIST)
        응답 = 폴백._run_secondary(request(requirements=요구))
        assert 응답.ok is False
        assert 이차.built == [], "명령을 만들었다는 것은 실행 경로에 들어갔다는 뜻이다"


class Test감사_종류:
    def test_보장_기록은_사고로_세지_않는다(self) -> None:
        """기준 통행량이라 사고 집계에 들어가면 사고율이 전부 바뀐다."""
        assert IncidentKind.CAPABILITY not in INCIDENT_KINDS


# 3단계 — 도구 제한 불일치를 실행 전 실패로 만든다. 다른 축은 아직 안 막는다.


class _준비기록엔진(RecordingEngine):
    """prepare 가 불렸는지 본다. 검증은 그 부수 효과보다 앞서야 한다."""

    capabilities = EngineCapabilities(tool_restriction=ToolRestriction.NONE)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.준비호출 = 0

    def prepare(self, request: Any) -> None:
        self.준비호출 += 1


class Test도구_제한을_못_맞추면_실행_전에_막는다:
    def _돌린다(self, tmp_path: Path, 감사: _감사 | None = None, **요청: Any) -> tuple[Any, Any, list[int]]:
        from test_engine import FakeCompleted, 통과정책

        실행 = []
        엔진 = _준비기록엔진(claude_profile(tmp_path), SETTINGS)

        def 프로세스(cmd: Any, cwd: Any, timeout: Any, env: Any = None) -> Any:
            실행.append(1)
            return FakeCompleted(stdout="답변", returncode=0)

        runner = EngineRunner(
            SETTINGS,
            subprocess_runner=프로세스,
            environment_policy=통과정책(),
            audit=감사,
        )
        return runner.run(엔진, request(**요청)), 엔진, 실행

    def test_프로세스를_띄우지_않고_실패로_돌려준다(self, tmp_path: Path) -> None:
        응답, 엔진, 실행 = self._돌린다(
            tmp_path,
            requirements=ExecutionRequirements(tool_restriction=ToolRestriction.EXACT_ALLOWLIST),
        )
        assert 응답.ok is False
        assert 응답.failure_reason == "capability_unmet"
        assert 응답.failure_detail is not None
        assert 응답.failure_detail.code == "tool_restriction"
        assert 실행 == []
        assert 엔진.built == []

    def test_prepare_의_부수효과도_내지_않는다(self, tmp_path: Path) -> None:
        _, 엔진, _ = self._돌린다(
            tmp_path,
            requirements=ExecutionRequirements(tool_restriction=ToolRestriction.EXACT_ALLOWLIST),
        )
        assert 엔진.준비호출 == 0

    def test_완화를_명시하면_그대로_실행한다(self, tmp_path: Path) -> None:
        응답, _, 실행 = self._돌린다(
            tmp_path,
            감사=_감사(),
            requirements=ExecutionRequirements(
                tool_restriction=ToolRestriction.EXACT_ALLOWLIST,
                downgradable_axes=frozenset({TOOL_AXIS}),
            ),
        )
        assert 응답.ok is True
        assert 실행 == [1]

    def test_감사할_곳이_없으면_완화를_안_받아준다(self, tmp_path: Path) -> None:
        """이름이 audited downgrade 다. 기록이 안 남는데 완화만 해 주면 그
        이름이 거짓이 된다 (sca-gpe)."""
        응답, 엔진, 실행 = self._돌린다(
            tmp_path,
            감사=None,
            requirements=ExecutionRequirements(
                tool_restriction=ToolRestriction.EXACT_ALLOWLIST,
                downgradable_axes=frozenset({TOOL_AXIS}),
            ),
        )
        assert 응답.ok is False
        assert 실행 == []
        assert 엔진.준비호출 == 0

    def test_감사_경로_부재는_안내문에_드러난다(self, tmp_path: Path) -> None:
        """엔진 제약만 말하면 배선 문제를 엔진 탓으로 읽는다. 완화를 요청했는데
        기록할 곳이 없어 막힌 것이 이 차단의 직접 조건이다."""
        응답, _, _ = self._돌린다(
            tmp_path,
            감사=None,
            requirements=ExecutionRequirements(
                tool_restriction=ToolRestriction.EXACT_ALLOWLIST,
                downgradable_axes=frozenset({TOOL_AXIS}),
            ),
        )
        assert "감사 기록기가 구성되지 않아" in 응답.body

    def test_완화를_요청하지_않은_차단에는_감사_문구가_없다(self, tmp_path: Path) -> None:
        응답, _, _ = self._돌린다(
            tmp_path,
            감사=None,
            requirements=ExecutionRequirements(tool_restriction=ToolRestriction.EXACT_ALLOWLIST),
        )
        assert 응답.ok is False
        assert "감사 기록기" not in 응답.body

    def test_도구_외_축도_막는다(self, tmp_path: Path) -> None:
        """선언한 축을 하나만 강제하면 나머지 둘은 요구해도 그냥 돈다.
        요구를 세우는 쪽은 세 축이 같은 무게라고 읽는다 (sca-igu)."""
        응답, _, 실행 = self._돌린다(
            tmp_path,
            requirements=ExecutionRequirements(
                execution_isolation=ExecutionIsolation.READONLY_SANDBOX,
                instruction_boundary=InstructionBoundary.NATIVE,
            ),
        )
        assert 응답.ok is False
        assert 실행 == []

    def test_안내문에_내부_식별자를_쓰지_않는다(self, tmp_path: Path) -> None:
        """사람이 읽는 문구다. exact_allowlist 같은 값은 내부 이름이다."""
        응답, _, _ = self._돌린다(
            tmp_path,
            requirements=ExecutionRequirements(tool_restriction=ToolRestriction.EXACT_ALLOWLIST),
        )
        assert "exact_allowlist" not in 응답.body
        assert "허용된 도구 목록" in 응답.body

    def test_막은_축이_감사에_읽히는_값으로_남는다(self, tmp_path: Path) -> None:
        """코드가 허용 목록에 없으면 unknown:N 으로 적혀 축 정보가 사라진다."""
        from slack_cli_agent.engine.base import KNOWN_DETAIL_CODES

        응답, _, _ = self._돌린다(
            tmp_path,
            requirements=ExecutionRequirements(
                execution_isolation=ExecutionIsolation.READONLY_SANDBOX,
            ),
        )
        assert 응답.failure_detail is not None
        assert 응답.failure_detail.code in KNOWN_DETAIL_CODES

    def test_못_맞춘_축을_안내문에_적는다(self, tmp_path: Path) -> None:
        응답, _, _ = self._돌린다(
            tmp_path,
            requirements=ExecutionRequirements(
                execution_isolation=ExecutionIsolation.READONLY_SANDBOX,
            ),
        )
        assert (응답.failure_detail.code if 응답.failure_detail else "") == "execution_isolation"
        assert "격리" in 응답.body

    def test_맞춘_축만_요구하면_그대로_실행한다(self, tmp_path: Path) -> None:
        응답, _, 실행 = self._돌린다(
            tmp_path,
            requirements=ExecutionRequirements(
                execution_isolation=ExecutionIsolation.NONE,
            ),
        )
        assert 응답.ok is True
        assert 실행 == [1]

    def test_요구가_없으면_그대로_실행한다(self, tmp_path: Path) -> None:
        응답, _, 실행 = self._돌린다(tmp_path)
        assert 응답.ok is True
        assert 실행 == [1]

    def test_막을_때도_감사_기록은_남는다(self, tmp_path: Path) -> None:
        감사 = _감사()
        self._돌린다(
            tmp_path,
            감사,
            requirements=ExecutionRequirements(tool_restriction=ToolRestriction.EXACT_ALLOWLIST),
        )
        [(kind, 필드)] = 감사.기록
        assert kind == CAPABILITY_KIND
        assert 필드["unmet"] == ["tool_restriction"]

    def test_차단_사유가_알려진_코드다(self, tmp_path: Path) -> None:
        """unknown:N 으로 찍히면 로그에서 무엇이 막혔는지 못 읽는다."""
        응답, _, _ = self._돌린다(
            tmp_path,
            requirements=ExecutionRequirements(tool_restriction=ToolRestriction.EXACT_ALLOWLIST),
        )
        assert 응답.failure_detail is not None
        assert "unknown" not in str(응답.failure_detail)
