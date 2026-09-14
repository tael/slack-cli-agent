"""review/engine_adapter.py 의 ReviewEngineCaller 테스트.

점검(부검·디버그 추적·서식 점검)은 EngineRequest 를 사람 대화와 다른 값으로
조립해야 한다 — workdir 은 봇 코드/지침 경로, model/effort 는 소유자 수준,
trust_level 은 OWNER 고정. 이 어댑터가 그 조립만 맡고 실행은 EngineRunner 에
위임하는지를 검증한다.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from slack_cli_agent.auth.policy import OWNER_EFFORT_MIN
from slack_cli_agent.auth.principal import TrustLevel
from slack_cli_agent.config.profile import Profile
from slack_cli_agent.engine.base import Engine, EngineRequest, EngineResponse, UsageLimit
from slack_cli_agent.review.base import EngineCaller
from slack_cli_agent.review.engine_adapter import ReviewEngineCaller


def make_profile(**overrides) -> Profile:
    data = {
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


class FakeRunner:
    """EngineRunner 대역. 넘어온 request 를 그대로 기록하고 정해 둔 응답을 돌려준다."""

    def __init__(self, response: EngineResponse) -> None:
        self._response = response
        self.calls: list[tuple[Engine, EngineRequest]] = []

    def run(self, engine: Engine, request: EngineRequest) -> EngineResponse:
        self.calls.append((engine, request))
        return self._response


def _response(ok: bool = True, body: str = "결과") -> EngineResponse:
    return EngineResponse(
        ok=ok, body=body, session_id="s1" if ok else None, model_actual=None,
        elapsed=1.0, turns=1 if ok else None, usage=None,
        failure_reason=None if ok else "nonzero_exit",
    )


def make_caller(**overrides) -> tuple[ReviewEngineCaller, FakeRunner]:
    profile = overrides.pop("profile", None) or make_profile()
    runner = overrides.pop("runner", None) or FakeRunner(_response())
    engine = overrides.pop("engine", None) or FakeEngine(profile, None)
    kwargs = dict(
        engine=engine,
        runner=runner,
        profile=profile,
        workdir=Path("/code"),
        system_prompt="시스템 프롬프트",
        readable_dirs=(Path("/persona"),),
        allowed_tools=("Read",),
    )
    kwargs.update(overrides)
    return ReviewEngineCaller(**kwargs), runner


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
        caller, runner = make_caller()
        caller.run("프롬프트", "세션1", False)
        assert len(runner.calls) == 1
        _, request = runner.calls[0]
        assert request.prompt == "프롬프트"
        assert request.session_id == "세션1"
        assert request.resume is False
        assert request.system_prompt == "시스템 프롬프트"
        assert request.workdir == Path("/code")
        assert request.readable_dirs == (Path("/persona"),)
        assert request.allowed_tools == ("Read",)

    def test_resume_True로부르면_EngineRequest_resume도_True다(self) -> None:
        caller, runner = make_caller()
        caller.run("프롬프트", "세션1", True)
        _, request = runner.calls[0]
        assert request.resume is True

    def test_trust_level은_OWNER로_고정된다(self) -> None:
        caller, runner = make_caller()
        caller.run("프롬프트", "세션1", False)
        _, request = runner.calls[0]
        assert request.trust_level is TrustLevel.OWNER

    def test_engine을_runner에_그대로넘긴다(self) -> None:
        profile = make_profile()
        engine = FakeEngine(profile, None)
        caller, runner = make_caller(profile=profile, engine=engine)
        caller.run("프롬프트", "세션1", False)
        used_engine, _ = runner.calls[0]
        assert used_engine is engine


class Test기본값:
    def test_model과effort를안주면소유자수준기본값을쓴다(self) -> None:
        profile = make_profile()
        caller, runner = make_caller(profile=profile)
        caller.run("프롬프트", "세션1", False)
        _, request = runner.calls[0]
        assert request.model == profile.primary_engine.model_for_owner()
        assert request.effort == OWNER_EFFORT_MIN

    def test_model과effort를생성자에서받으면그값을쓴다(self) -> None:
        caller, runner = make_caller(model="opus-custom", effort="max")
        caller.run("프롬프트", "세션1", False)
        _, request = runner.calls[0]
        assert request.model == "opus-custom"
        assert request.effort == "max"

    def test_readable_dirs와allowed_tools기본값은빈튜플이다(self) -> None:
        profile = make_profile()
        caller = ReviewEngineCaller(
            engine=FakeEngine(profile, None),
            runner=FakeRunner(_response()),
            profile=profile,
            workdir=Path("/code"),
            system_prompt="프롬프트",
        )
        caller.run("프롬프트", "세션1", False)


class Test실패응답:
    def test_엔진이실패해도예외를내지않고응답을그대로돌려준다(self) -> None:
        failure = _response(ok=False, body="한도 초과")
        caller, _ = make_caller(runner=FakeRunner(failure))
        response = caller.run("프롬프트", "세션1", False)
        assert response.ok is False
        assert response.body == "한도 초과"
        assert response is failure
