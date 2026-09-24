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
from slack_cli_agent.engine.tool_selection import ToolSelection
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
        보장 = 엔진.capabilities_for(request(tools=ToolSelection.allow(["Read", "Grep"])))
        assert 보장.tool_restriction is ToolRestriction.EXACT_ALLOWLIST
        assert 보장.instruction_boundary is InstructionBoundary.NATIVE

    def test_claude_도_허용목록이_비면_도구_제한이_없다(self, tmp_path: Path) -> None:
        """--allowedTools 를 빈 문자열로 넘긴 것은 제한을 건 것이 아니다."""
        엔진 = ClaudeEngine(claude_profile(tmp_path), SETTINGS)
        보장 = 엔진.capabilities_for(request(tools=ToolSelection.unrestricted()))
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
        보장 = 엔진.capabilities_for(request(model="gpt-5", tools=ToolSelection.unrestricted()))
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
        보장 = 엔진.capabilities_for(request(model="gpt-5", tools=ToolSelection.unrestricted()))
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

    @property
    def 보장기록(self) -> list[tuple[str, dict[str, Any]]]:
        """전송량 기록도 같은 포트로 들어온다. 보장 쪽만 본다."""
        return [(kind, 필드) for kind, 필드 in self.기록 if kind == "capability"]


class _기록실패감사:
    """AuditLog 처럼 기록에 실패하면 예외를 낸다."""

    def __init__(self) -> None:
        self.시도: list[str] = []

    def record(self, kind: str, *, channel: str = "", thread_ts: str = "", **fields: Any) -> None:
        self.시도.append(kind)
        raise OSError("감사 파일을 쓰지 못했다")


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
        [(kind, 필드)] = 감사.보장기록
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
        assert 감사.보장기록[0][1]["unmet"] == ["tool_restriction", "instruction_boundary"]

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
        필드 = 감사.보장기록[0][1]
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
        assert 감사.보장기록[0][1]["downgrade_authorized"] is True

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
        assert 감사.보장기록[0][1]["downgrade_authorized"] is False

    @pytest.mark.parametrize(
        ("요구", "기대"),
        [
            (ExecutionRequirements(), "compatible"),
            (
                ExecutionRequirements(
                    tool_restriction=ToolRestriction.EXACT_ALLOWLIST,
                    downgradable_axes=frozenset({TOOL_AXIS}),
                ),
                "downgraded",
            ),
            (
                ExecutionRequirements(tool_restriction=ToolRestriction.EXACT_ALLOWLIST),
                "blocked",
            ),
        ],
    )
    def test_이_요청이_어떻게_끝났는지_한_값으로_남는다(
        self, tmp_path: Path, 요구: ExecutionRequirements, 기대: str
    ) -> None:
        """unmet 과 완화 목록을 조합해야 알 수 있으면 집계마다 그 규칙을 다시
        쓴다. 세는 쪽이 한 값을 읽게 한다."""
        감사 = _감사()
        self._돌린다(tmp_path, 감사, requirements=요구)
        assert 감사.보장기록[0][1]["outcome"] == 기대

    def test_요구가_없어도_실제_보장은_남는다(self, tmp_path: Path) -> None:
        """0건이 요구 없음인지 기록 자체가 안 도는 것인지 구분돼야 한다."""
        감사 = _감사()
        self._돌린다(tmp_path, 감사)
        [(_, 필드)] = 감사.보장기록
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
    def _돌린다(self, tmp_path: Path, 감사: Any = None, **요청: Any) -> tuple[Any, Any, list[int]]:
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

    def test_막힌_응답에도_요청_모델이_실린다(self, tmp_path: Path) -> None:
        """막힌 요청도 기록에 남는다. 그 줄만 모델 칸이 비면 어느 모델의
        요청이 막혔는지 못 센다 (sca-cr2b)."""
        응답, _, _ = self._돌린다(
            tmp_path,
            model="claude-opus-5",
            requirements=ExecutionRequirements(tool_restriction=ToolRestriction.EXACT_ALLOWLIST),
        )
        assert 응답.model_asked == "claude-opus-5"

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

    def test_기록이_실패하면_완화를_받아주지_않는다(self, tmp_path: Path) -> None:
        """audited downgrade 의 근거는 기록이 남았다는 사실이다. 포트가
        주입됐는지가 아니라 이번 기록이 실제로 남았는지를 본다 (sca-ckm)."""
        감사 = _기록실패감사()
        응답, 엔진, 실행 = self._돌린다(
            tmp_path,
            감사=감사,
            requirements=ExecutionRequirements(
                tool_restriction=ToolRestriction.EXACT_ALLOWLIST,
                downgradable_axes=frozenset({TOOL_AXIS}),
            ),
        )
        assert 감사.시도.count("capability") == 1
        assert 응답.ok is False
        assert 실행 == []
        assert 엔진.준비호출 == 0

    def test_기록_실패는_안내문에_드러난다(self, tmp_path: Path) -> None:
        응답, _, _ = self._돌린다(
            tmp_path,
            감사=_기록실패감사(),
            requirements=ExecutionRequirements(
                tool_restriction=ToolRestriction.EXACT_ALLOWLIST,
                downgradable_axes=frozenset({TOOL_AXIS}),
            ),
        )
        assert "감사 기록을 남기지 못해" in 응답.body

    def test_기록이_실패해도_요구를_맞춘_요청은_그대로_실행한다(self, tmp_path: Path) -> None:
        """감사는 완화의 근거일 뿐이다. 완화가 필요 없는 요청까지 기록 실패로
        막으면 감사 장애가 곧 서비스 중단이 된다."""
        응답, _, 실행 = self._돌린다(
            tmp_path,
            감사=_기록실패감사(),
            requirements=ExecutionRequirements(),
        )
        assert 응답.ok is True
        assert 실행 == [1]

    def test_기록은_prepare_보다_먼저다(self, tmp_path: Path) -> None:
        """기록이 실행 뒤면 완화 판단이 기록을 근거로 못 선다. 순서를 시험으로
        고정한다 (sca-ckm 리뷰)."""
        감사 = _감사()
        _, 엔진, _ = self._돌린다(
            tmp_path,
            감사=감사,
            requirements=ExecutionRequirements(
                tool_restriction=ToolRestriction.EXACT_ALLOWLIST,
                downgradable_axes=frozenset({TOOL_AXIS}),
            ),
        )
        assert 엔진.준비호출 == 1
        assert len(감사.보장기록) == 1

    def test_완화_기록은_승인_시점의_사실로_남는다(self, tmp_path: Path) -> None:
        """기록은 실행 전에 남는다. 실행이 뒤에 실패해도 그 값은 '완화를
        승인했다' 는 뜻이지 '완화된 채로 끝났다' 가 아니다 (sca-ckm 리뷰)."""
        감사 = _감사()
        self._돌린다(
            tmp_path,
            감사=감사,
            requirements=ExecutionRequirements(
                tool_restriction=ToolRestriction.EXACT_ALLOWLIST,
                downgradable_axes=frozenset({TOOL_AXIS}),
            ),
        )
        _, 필드 = 감사.보장기록[0]
        assert 필드["downgrade_authorized"] is True
        assert "downgrade_applied" not in 필드

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
        [(kind, 필드)] = 감사.보장기록
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


class Test전환_승인_안내는_요구_충족까지_말한다:
    """프로브는 '이차가 답하는가' 만 본다. 그것만 보고 승인하면 승인된 폴백이
    그 요청을 처리할 수 있다는 뜻이 아니게 된다 (sca-42s)."""

    def _전환한다(self, tmp_path: Path, 요구: ExecutionRequirements) -> dict[str, Any]:
        from engine_support import named
        from test_engine import FakeCompleted, RecordingEngine, profile_with, request, 통과정책

        from slack_cli_agent.engine.base import EngineResponse

        한도 = EngineResponse(
            ok=False, body="한도 소진", session_id=None, model_actual=None,
            elapsed=0, turns=None, usage=None, failure_reason="usage_limit",
        )
        프로필 = profile_with(
            {"type": "claude", "binary": "claude", "model": "claude-sonnet-5"},
            {"type": "gemini", "binary": "agy", "model": "gemini-3.8-flash"},
            tmp_path=tmp_path,
        )
        일차 = named(RecordingEngine, "claude", capabilities=ClaudeEngine.capabilities)(
            프로필, SETTINGS, response=한도
        )
        이차 = named(RecordingEngine, "gemini", capabilities=GeminiEngine.capabilities)(
            프로필, SETTINGS
        )
        switcher = EngineSwitcher(tmp_path / "engine_state.json")
        runner = EngineRunner(
            SETTINGS,
            subprocess_runner=lambda cmd, cwd, timeout, env=None: FakeCompleted(
                stdout="답변", returncode=0
            ),
            environment_policy=통과정책(),
        )
        FallbackEngine(일차, 이차, switcher, runner).run(request(requirements=요구))
        return switcher.load()

    def test_이차가_요구를_못_맞추면_안내에_적는다(self, tmp_path: Path) -> None:
        상태 = self._전환한다(
            tmp_path,
            ExecutionRequirements(tool_restriction=ToolRestriction.EXACT_ALLOWLIST),
        )
        assert "도구 제한" in 상태["probe_detail"]

    def test_요구를_맞추면_그런_말을_안_적는다(self, tmp_path: Path) -> None:
        상태 = self._전환한다(tmp_path, ExecutionRequirements())
        assert "도구 제한" not in 상태["probe_detail"]


class Test보장_기록에_상관관계_키가_남는다:
    """폴백·복구 프로브·재시도가 한 요청의 시도라는 것을 집계 쪽에서 알
    방법이 지금 없다 (sca-4ol)."""

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

    def test_요청의_키가_그대로_남는다(self, tmp_path: Path) -> None:
        감사 = _감사()
        self._돌린다(tmp_path, 감사, request_id="req-1")
        assert 감사.보장기록[0][1]["request_id"] == "req-1"

    def test_키가_없으면_빈_값으로_남는다(self, tmp_path: Path) -> None:
        """없는 것과 안 남긴 것이 갈려야 한다. 필드 자체는 항상 있다."""
        감사 = _감사()
        self._돌린다(tmp_path, 감사)
        assert 감사.보장기록[0][1]["request_id"] == ""


class Test폴백_시도도_같은_키로_묶인다:
    """일차가 한도에 걸려 이차로 넘어간 것은 같은 요청의 다음 시도다. 키가
    끊기면 집계에서 별개 요청 2건으로 보인다 (sca-4ol)."""

    def test_이차로_넘길_때_키를_버리지_않는다(self, tmp_path: Path) -> None:
        from engine_support import named
        from test_engine import RecordingEngine, profile_with, 통과정책

        프로필 = profile_with(
            {"type": "claude", "binary": "claude", "model": "claude-sonnet-5"},
            {"type": "gemini", "binary": "agy", "model": "gemini-3.8-flash"},
            tmp_path=tmp_path,
        )
        이차종류 = named(RecordingEngine, "gemini", capabilities=GeminiEngine.capabilities)
        폴백 = FallbackEngine(
            ClaudeEngine(프로필, SETTINGS), 이차종류(프로필, SETTINGS),
            EngineSwitcher(tmp_path / "engine_state.json"),
            EngineRunner(SETTINGS, environment_policy=통과정책()),
        )
        받은: list[Any] = []

        def 받아둔다(engine: Any, req: Any) -> Any:
            받은.append(req)
            raise _그만()

        폴백.runner.run = 받아둔다  # type: ignore[assignment,method-assign]
        with pytest.raises(_그만):
            폴백._run_secondary(request(request_id="req-9"))
        assert 받은[0].request_id == "req-9"


class Test허용목록_축은_MCP_차단_단위까지_본다:
    """--tools 는 내장 도구 집합만 닫는다. 허용목록이 어느 MCP 서버의 도구를
    하나라도 이름하면 그 서버는 통째로 열린 채 남고(sca-6ewc 실측), 그 서버의
    나머지 도구는 승인 규칙에만 맡겨진다. 그 턴을 exact_allowlist 로 적으면
    감사 기록이 실제보다 강하게 남는다 (sca-vo05).
    """

    def _보장(
        self, tmp_path: Path, names: list[str], mcp_servers: dict[str, Any] | None = None
    ) -> EngineCapabilities:
        profile = profile_with(
            {"type": "claude", "binary": "claude", "model": "claude-sonnet-5"},
            tmp_path=tmp_path, mcp_servers=mcp_servers,
        )
        engine = ClaudeEngine(profile, SETTINGS)
        return engine.capabilities_for(request(tools=ToolSelection.allow(names)))

    def test_MCP_서버가_열린_채_남으면_서버단위로_내려간다(self, tmp_path: Path) -> None:
        보장 = self._보장(
            tmp_path, ["Read", "mcp__jira__jira_search"],
            {"jira": {"command": "jira-mcp"}, "github": {"command": "gh-mcp"}},
        )
        assert 보장.tool_restriction is ToolRestriction.SERVER_SCOPED_ALLOWLIST

    def test_MCP_를_통째로_닫으면_정확한_허용목록이다(self, tmp_path: Path) -> None:
        보장 = self._보장(tmp_path, ["Read"], {"jira": {"command": "jira-mcp"}})
        assert 보장.tool_restriction is ToolRestriction.EXACT_ALLOWLIST

    def test_붙은_서버가_없으면_정확한_허용목록이다(self, tmp_path: Path) -> None:
        보장 = self._보장(tmp_path, ["Read"])
        assert 보장.tool_restriction is ToolRestriction.EXACT_ALLOWLIST

    def test_꺼둔_서버는_열린_서버로_안_센다(self, tmp_path: Path) -> None:
        """--mcp-config 에 안 실리므로 그 이름으로 열리는 도구가 없다."""
        보장 = self._보장(
            tmp_path, ["mcp__jira__jira_search"],
            {"jira": {"command": "jira-mcp", "disabled": True}},
        )
        assert 보장.tool_restriction is ToolRestriction.EXACT_ALLOWLIST

    def test_서버단위는_정확한_허용목록_요구를_못_채운다(self, tmp_path: Path) -> None:
        보장 = self._보장(
            tmp_path, ["mcp__jira__jira_search"], {"jira": {"command": "jira-mcp"}}
        )
        요구 = ExecutionRequirements(tool_restriction=ToolRestriction.EXACT_ALLOWLIST)
        assert 요구.unmet(보장) == ("tool_restriction",)

    def test_서버단위도_샌드박스_수준보다는_세다(self) -> None:
        """축을 새로 끼워 넣은 자리가 맞는지 본다. 내장 도구는 정확히 닫힌다."""
        보장 = EngineCapabilities(tool_restriction=ToolRestriction.SERVER_SCOPED_ALLOWLIST)
        요구 = ExecutionRequirements(tool_restriction=ToolRestriction.COARSE_SANDBOX)
        assert 요구.unmet(보장) == ()

    def test_사람이_읽는_이름이_있다(self) -> None:
        """없으면 슬랙 알림에 내부 식별자가 그대로 나간다."""
        from slack_cli_agent.engine.runner import LEVEL_NAMES

        assert LEVEL_NAMES[ToolRestriction.SERVER_SCOPED_ALLOWLIST] == "허용 목록, MCP 는 서버 단위"


class Test기동_판정도_MCP_서버를_본다:
    """기동 점검은 요청이 없어 configured_capabilities 를 본다. 그 값이
    프로필의 MCP 서버를 안 보면 요청 시 판정(capabilities_for)과 어긋난다 —
    붙은 서버가 있는 claude 프로필은 요청마다 서버 단위로 내려가는데 기동
    때는 정확한 허용목록으로 적혔다 (sca-qqtl).
    """

    def _프로필(self, tmp_path: Path, mcp_servers: dict[str, Any] | None = None) -> Profile:
        return profile_with(
            {"type": "claude", "binary": "claude", "model": "claude-sonnet-5"},
            tmp_path=tmp_path, mcp_servers=mcp_servers,
        )

    def test_붙은_서버가_없으면_정확한_허용목록이다(self, tmp_path: Path) -> None:
        profile = self._프로필(tmp_path)
        보장 = ClaudeEngine.configured_capabilities(profile.primary_engine, profile.mcp_servers)
        assert 보장.tool_restriction is ToolRestriction.EXACT_ALLOWLIST

    def test_서버가_붙어_있으면_서버단위로_내려간다(self, tmp_path: Path) -> None:
        """어느 서버가 열릴지는 요청의 허용목록이 정한다. 기동 때는 어느
        요청도 없으므로 열릴 수 있다는 사실 자체가 그 프로필의 상한이다."""
        profile = self._프로필(tmp_path, {"jira": {"command": "jira-mcp"}})
        보장 = ClaudeEngine.configured_capabilities(profile.primary_engine, profile.mcp_servers)
        assert 보장.tool_restriction is ToolRestriction.SERVER_SCOPED_ALLOWLIST

    def test_꺼둔_서버만_있으면_정확한_허용목록이다(self, tmp_path: Path) -> None:
        """--mcp-config 에 안 실리므로 그 이름으로 열리는 도구가 없다."""
        profile = self._프로필(tmp_path, {"jira": {"command": "jira-mcp", "disabled": True}})
        보장 = ClaudeEngine.configured_capabilities(profile.primary_engine, profile.mcp_servers)
        assert 보장.tool_restriction is ToolRestriction.EXACT_ALLOWLIST

    def test_MCP_를_안_받는_엔진은_서버가_있어도_그대로다(self, tmp_path: Path) -> None:
        """codex 의 축은 샌드박스다. 서버 유무가 그 축을 바꾸지 않는다."""
        profile = profile_with(
            {"type": "codex", "binary": "codex", "model": "gpt-5",
             "options": {"sandbox": "read-only"}},
            tmp_path=tmp_path, mcp_servers={"jira": {"command": "jira-mcp"}},
        )
        보장 = CodexEngine.configured_capabilities(profile.primary_engine, profile.mcp_servers)
        assert 보장.tool_restriction is ToolRestriction.COARSE_SANDBOX
