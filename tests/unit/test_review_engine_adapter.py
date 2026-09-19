"""review/engine_adapter.py 의 ReviewEngineCaller 테스트.

점검(부검·디버그 추적·서식 점검)은 EngineRequest 를 사람 대화와 다른 값으로
조립해야 한다 — workdir 은 봇 코드/지침 경로, model/effort 는 소유자 수준,
trust_level 은 OWNER 고정. 이 어댑터가 그 조립만 맡고 실행은 EngineInvoker 에
위임하는지를 검증한다.

실행기(EngineRunner)를 직접 받지 않는다 — 폴백이 설정돼 있어도 EngineRunner.run()
을 부르면 FallbackEngine.run() 의 한도 감지와 전환 기록이 건너뛰어져, 점검 리액션
경로만 1차 엔진에 계속 묶인다.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from slack_cli_agent.auth.policy import OWNER_EFFORT_MIN
from slack_cli_agent.auth.principal import TrustLevel
from slack_cli_agent.config.profile import Profile
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.engine.base import (
    CallOrigin,
    Engine,
    EngineRequest,
    EngineResponse,
    UsageLimit,
)
from slack_cli_agent.engine.capability import ToolRestriction
from slack_cli_agent.engine.runner import DirectInvoker, EngineInvoker, EngineRunner
from slack_cli_agent.review.base import EngineCaller
from slack_cli_agent.review.engine_adapter import ReviewEngineCaller


def make_profile(**overrides) -> Profile:
    data: dict[str, Any] = {
        "name": "테스트봇",
        "primary_engine": {
            "type": "claude",
            "binary": "/usr/bin/true",
            "model": "sonnet",
            "model_owner": "opus",
        },
        "owner_user_id": "U1",
        "troubleshoot_channel": "TS",
    }
    data.update(overrides)
    return Profile.from_dict(data)


class FakeEngine(Engine):
    """실제 Engine 계약을 만족하는 최소 가짜. build_command/parse 는 안 쓰인다."""

    name = "fake"

    def build_command(self, request: EngineRequest) -> list[str]:
        raise NotImplementedError

    def parse(self, stdout: str, stderr: str, returncode: int) -> EngineResponse:
        raise NotImplementedError

    def new_session_id(self) -> str:
        return "engine-session"

    def detect_usage_limit(self, response: EngineResponse) -> UsageLimit | None:
        return None


class FakeInvoker(EngineInvoker):
    """EngineInvoker 대역. 넘어온 request 를 그대로 기록하고 정해 둔 응답을 돌려준다."""

    def __init__(self, response: EngineResponse) -> None:
        self._response = response
        self.calls: list[EngineRequest] = []
        self.origins: list[CallOrigin] = []

    def invoke(self, request: EngineRequest,
               origin: CallOrigin = CallOrigin.INTERACTIVE) -> EngineResponse:
        self.calls.append(request)
        self.origins.append(origin)
        return self._response


def _response(ok: bool = True, body: str = "결과") -> EngineResponse:
    return EngineResponse(
        ok=ok, body=body, session_id="s1" if ok else None, model_actual=None,
        elapsed=1.0, turns=1 if ok else None, usage=None,
        failure_reason=None if ok else "nonzero_exit",
    )


def make_caller(**overrides) -> tuple[ReviewEngineCaller, FakeInvoker]:
    profile = overrides.pop("profile", None) or make_profile()
    invoker = overrides.pop("invoker", None) or FakeInvoker(_response())
    kwargs: dict[str, Any] = {
        "invoker": invoker,
        "profile": profile,
        "workdir": Path("/code"),
        "system_prompt": "시스템 프롬프트",
        "readable_dirs": (Path("/persona"),),
        "allowed_tools": ("Read",),
    }
    kwargs.update(overrides)
    return ReviewEngineCaller(**kwargs), invoker


class TestProtocol준수:
    def test_EngineCaller_Protocol을만족한다(self) -> None:
        # EngineCaller 는 @runtime_checkable 이 아니라 isinstance 판정이 안 된다
        # (다른 review 시험 파일도 이 Protocol 을 isinstance 로 검사하지 않는다).
        # run() 이 Protocol 계약과 같은 시그니처(prompt, session_id, resume)로
        # 존재하는지, ReviewTask 가 기대하는 자리에 실제로 꽂히는지로 구조적으로
        # 확인한다.
        caller, _ = make_caller()
        params = list(inspect.signature(caller.run).parameters)
        protocol_params = list(inspect.signature(EngineCaller.run).parameters)[1:]
        assert params == protocol_params
        response = caller.run("프롬프트", "세션1", False)
        assert isinstance(response, EngineResponse)


class TestEngineRequest조립:
    def test_인자로받은값이그대로전달된다(self) -> None:
        caller, invoker = make_caller()
        caller.run("프롬프트", "세션1", False)
        assert len(invoker.calls) == 1
        request = invoker.calls[0]
        assert request.prompt == "프롬프트"
        assert request.session_id == "세션1"
        assert request.resume is False
        assert request.system_prompt == "시스템 프롬프트"
        assert request.workdir == Path("/code")
        assert request.readable_dirs == (Path("/persona"),)
        assert request.allowed_tools == ("Read",)

    def test_도구를_준_요청에는_실행_보장_요구가_붙는다(self) -> None:
        """요구를 세우는 자리가 파이프라인만이면 이 경로의 도구 권한은 아무도
        강제하지 않는다 (sca-98k)."""
        caller, invoker = make_caller()
        caller.run("프롬프트", None, False)
        요구 = invoker.calls[0].requirements
        assert 요구.tool_restriction is ToolRestriction.EXACT_ALLOWLIST
        assert 요구.allow_audited_downgrade is True

    def test_도구가_없으면_요구도_없다(self) -> None:
        caller, invoker = make_caller(allowed_tools=())
        caller.run("프롬프트", None, False)
        assert invoker.calls[0].requirements.tool_restriction is None

    def test_resume_True로부르면_EngineRequest_resume도_True다(self) -> None:
        caller, invoker = make_caller()
        caller.run("프롬프트", "세션1", True)
        request = invoker.calls[0]
        assert request.resume is True

    def test_trust_level은_OWNER로_고정된다(self) -> None:
        caller, invoker = make_caller()
        caller.run("프롬프트", "세션1", False)
        request = invoker.calls[0]
        assert request.trust_level is TrustLevel.OWNER

    def test_주입받은_invoker로_실행한다(self) -> None:
        """실행기를 직접 받아 엔진을 고르지 않는다. 어느 엔진을 쓸지는 조립이 정한다."""
        profile = make_profile()
        engine = FakeEngine(profile, RuntimeSettings())
        invoker = DirectInvoker(EngineRunner(RuntimeSettings()), engine)
        caller, _ = make_caller(profile=profile, invoker=invoker)
        assert caller._invoker is invoker

    def test_실행기를_직접_받지_않는다(self) -> None:
        """생성자에 runner/engine 을 받으면 호출부가 폴백을 우회할 수 있다."""
        params = set(inspect.signature(ReviewEngineCaller.__init__).parameters)
        assert "runner" not in params
        assert "engine" not in params
        assert "invoker" in params


class Test기본값:
    def test_model과effort를안주면소유자수준기본값을쓴다(self) -> None:
        profile = make_profile()
        caller, invoker = make_caller(profile=profile)
        caller.run("프롬프트", "세션1", False)
        request = invoker.calls[0]
        assert request.model == profile.primary_engine.model_for_owner()
        assert request.effort == OWNER_EFFORT_MIN

    def test_model과effort를생성자에서받으면그값을쓴다(self) -> None:
        caller, invoker = make_caller(model="opus-custom", effort="max")
        caller.run("프롬프트", "세션1", False)
        request = invoker.calls[0]
        assert request.model == "opus-custom"
        assert request.effort == "max"

    def test_readable_dirs와allowed_tools기본값은빈튜플이다(self) -> None:
        profile = make_profile()
        invoker = FakeInvoker(_response())
        caller = ReviewEngineCaller(
            invoker=invoker,
            profile=profile,
            workdir=Path("/code"),
            system_prompt="프롬프트",
        )
        caller.run("프롬프트", "세션1", False)
        request = invoker.calls[0]
        assert request.readable_dirs == ()
        assert request.allowed_tools == ()


class Test실패응답:
    def test_엔진이실패해도예외를내지않고응답을그대로돌려준다(self) -> None:
        failure = _response(ok=False, body="한도 초과")
        caller, _ = make_caller(invoker=FakeInvoker(failure))
        response = caller.run("프롬프트", "세션1", False)
        assert response.ok is False
        assert response.body == "한도 초과"
        assert response is failure


class Test진행_로그_전달:
    """점검도 진행 표시를 받는다(sca-tfd). 엔진의 도구 훅이 이 파일에 쓴다."""

    def test_받은_경로를_요청에_넣는다(self) -> None:
        caller, invoker = make_caller()
        caller.run("프롬프트", "세션1", False, Path("/tmp/진행.log"))
        assert invoker.calls[0].progress_log == Path("/tmp/진행.log")

    def test_안_주면_진행_로그가_없다(self) -> None:
        caller, invoker = make_caller()
        caller.run("프롬프트", "세션1", False)
        assert invoker.calls[0].progress_log is None
