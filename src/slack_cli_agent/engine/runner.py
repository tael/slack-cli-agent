"""엔진 실행과 전환을 감싸는 계층.

``EngineRunner`` 는 subprocess 실행만 맡는다. 명령줄 조립(``build_command``)과
출력 파싱(``parse``)은 Engine 이 순수 함수로 제공하므로, 여기서는 그 둘 사이의
실행 한 걸음만 두고 테스트에서 subprocess 를 대역으로 주입할 수 있게 한다.

``FallbackEngine`` 은 ``EngineRunner`` 를 써서 1차·2차 엔진 실행과 전환 판정을
감싼다. 호출부는 이 클래스 하나만 보면 된다 — 전환 여부를 몰라도 된다.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Mapping
from typing import Any

from ..config.settings import RuntimeSettings
from .base import Engine, EngineRequest, EngineResponse, UsageLimit
from .environment import EngineEnvironmentPolicy
from .switcher import EngineSwitcher

SubprocessRunner = Callable[..., Any]


class EngineRunner:
    """엔진이 만든 명령줄을 subprocess 로 실행하고 결과를 파싱한다.

    타임아웃 900초에는 근거가 있다 — 300초는 150건 중 2건을 잘랐고 중앙값은
    34초였다(RuntimeSettings.request_timeout_sec 기본값).
    """

    def __init__(self, settings: RuntimeSettings,
               subprocess_runner: SubprocessRunner | None = None,
               environment_policy: "EngineEnvironmentPolicy | None" = None,
               source_env: Mapping[str, str] | None = None) -> None:
        self._settings = settings
        self._run = subprocess_runner or self._default_runner
        # 환경 변수 격리 정책. 안 주면 환경을 안 넘겨 부모 프로세스의 것을
        # 그대로 물려받는다 — 정책을 안 주는 호출부가 아직 있어 기존 동작을
        # 유지한다. 빈 환경을 넘기면 엔진이 PATH 를 못 찾아 실행되지 않는다.
        self._environment_policy = environment_policy
        self._source_env = source_env

    def run(self, engine: Engine, request: EngineRequest,
           timeout_sec: float | None = None) -> EngineResponse:
        cmd = engine.build_command(request)
        timeout = timeout_sec if timeout_sec is not None else self._settings.request_timeout_sec
        # 정책이 없으면 env 인자 자체를 안 넘긴다. 넘기면 실행기를 대역으로
        # 주입하는 기존 호출부가 그 인자를 안 받아 실행 자체가 실패한다.
        extra: dict[str, Any] = {}
        if self._environment_policy is not None:
            source = self._source_env if self._source_env is not None else os.environ
            extra["env"] = self._environment_policy.build(source)
        try:
            completed = self._run(
                cmd, cwd=str(request.workdir), timeout=timeout, **extra,
            )
        except subprocess.TimeoutExpired:
            timeout_int = int(timeout)
            return EngineResponse(
                ok=False,
                body=f"시간이 오래 걸려 중단했습니다. {timeout_int}초 안에 끝나지 않았습니다.",
                session_id=None, model_actual=None,
                elapsed=timeout, turns=None, usage=None,
                raw={}, failure_reason="timeout",
            )
        return engine.parse(completed.stdout, completed.stderr, completed.returncode)

    @staticmethod
    def _default_runner(cmd: list[str], cwd: str, timeout: float, env: Mapping[str, str] | None = None):
        return subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            env=dict(env) if env is not None else None,
        )


class FallbackEngine(Engine):
    """1차 엔진이 한도 소진을 내면 2차 엔진에 위임한다.

    호출부는 Engine 하나만 본다. 전환 여부를 몰라도 된다.

    동작은 원본 bot.py 의 ``run_with_fallback()`` 그대로다 — 전환은 즉시 하고,
    사람이 승인하기 전에는 한도 안내만 답한다. ``EngineSwitcher`` 가
    ``engine_state.json`` 을 관리하고, 이 클래스가 실제 실행 분기를 맡는다.
    """

    name = "fallback"

    # 대체 실행기가 실제로 쓸 수 있는 상태인지 확인하는 짧은 요청. 원본
    # ENGINE_PROBE_PROMPT 와 같다.
    PROBE_PROMPT = "준비됐으면 OK 두 글자만 답해라."
    # 짧은 요청 전용 타임아웃. 원본 ENGINE_PROBE_TIMEOUT 과 같다. 전체 요청
    # 타임아웃(request_timeout_sec)을 그대로 쓰면 대체 실행기가 응답 없이
    # 걸렸을 때 사람이 기다리는 턴이 그만큼 길어진다.
    PROBE_TIMEOUT_SEC = 120.0

    def __init__(self, primary: Engine, secondary: Engine, switcher: EngineSwitcher,
               runner: EngineRunner) -> None:
        super().__init__(primary.profile, primary.settings)
        self.primary = primary
        self.secondary = secondary
        self.switcher = switcher
        self.runner = runner
        # build_command/parse 를 이 클래스에 직접 부르는 호출부를 위한
        # 위임 대상. run() 이 실제로 어느 엔진을 쓸지 정하고 갱신한다.
        self._active: Engine = primary

    # -- Engine 계약. EngineRunner 를 거치지 않고 이 클래스가 직접 build_command
    # /parse 로 쓰일 때를 위한 위임이다. 실제 호출부는 run() 을 쓴다.
    def build_command(self, request: EngineRequest) -> list[str]:
        return self._active.build_command(request)

    def parse(self, stdout: str, stderr: str, returncode: int) -> EngineResponse:
        return self._active.parse(stdout, stderr, returncode)

    def new_session_id(self) -> str:
        return self.primary.new_session_id()

    def detect_usage_limit(self, response: EngineResponse) -> UsageLimit | None:
        return self._active.detect_usage_limit(response)

    def session_id_from(self, response: EngineResponse) -> str | None:
        return self._active.session_id_from(response)

    def directives_for_turn(self, request: EngineRequest) -> str:
        return self._active.directives_for_turn(request)

    def readable_paths_note(self, paths):
        return self._active.readable_paths_note(paths)

    # -- 실제 진입점.
    def run(self, request: EngineRequest) -> EngineResponse:
        """전환 상태를 확인하고 1차 또는 2차로 실행한다."""
        state = self.switcher.load()

        if state:
            if self.switcher.should_probe(time.time()):
                recovered = self._probe_primary_recovery(request)
                if recovered is not None:
                    return recovered

            if not self.switcher.is_approved():
                self._active = self.primary
                return EngineResponse(
                    ok=False, body=self.switcher.limit_reply(), session_id=None,
                    model_actual=None, elapsed=0.0, turns=None, usage=None,
                    raw={"engine_switch": self.switcher.load().get("approval", "pending")},
                    failure_reason="usage_limit",
                )

            return self._run_secondary(request)

        return self._run_primary(request)

    def _run_primary(self, request: EngineRequest) -> EngineResponse:
        self._active = self.primary
        response = self.runner.run(self.primary, request)
        limit = self.primary.detect_usage_limit(response)
        if limit is not None:
            probe_ok, probe_detail = self._probe_secondary(request)
            self.switcher.begin_switch(
                limit.detail, engine_name=self.secondary.name,
                probe_ok=probe_ok, probe_detail=probe_detail,
            )
        return response

    def _run_secondary(self, request: EngineRequest) -> EngineResponse:
        """승인된 대체 엔진으로 이번 턴을 처리한다.

        엔진이 다르면 세션을 잇지 못한다. 새 세션으로 연다. 모델 이름 체계도
        엔진마다 달라 대체 엔진 자신의 설정값을 쓴다.
        """
        self._active = self.secondary
        fallback_request = EngineRequest(
            prompt=request.prompt, system_prompt=request.system_prompt,
            session_id=self.secondary.new_session_id(), resume=False,
            model=self.secondary.spec.model, effort=request.effort,
            workdir=request.workdir, readable_dirs=request.readable_dirs,
            allowed_tools=request.allowed_tools, trust_level=request.trust_level,
        )
        return self.runner.run(self.secondary, fallback_request)

    def _probe_primary_recovery(self, request: EngineRequest) -> EngineResponse | None:
        """기본 실행기가 돌아왔는지 실제 요청으로 떠본다.

        요청 경로에서만 한다. 사람이 기다리는 자리라 되돌아온 사실을 가장
        먼저 알아야 하고, 되돌린 뒤 그 턴을 바로 처리할 수 있다.
        """
        self._active = self.primary
        response = self.runner.run(self.primary, request)
        self.switcher.mark_probed(time.time())
        if response.ok and self.primary.detect_usage_limit(response) is None:
            self.switcher.recover()
            return response
        return None

    def _probe_secondary(self, request: EngineRequest) -> tuple[bool, str]:
        """대체 실행기가 실제로 답하는지 짧은 요청으로 확인한다.

        상태 파일만 바꾸고 전환했다고 알리면, 정작 그 실행기도 못 쓰는
        경우에 사람이 잘못된 상태를 믿는다.
        """
        probe_request = EngineRequest(
            prompt=self.PROBE_PROMPT, system_prompt="",
            session_id=self.secondary.new_session_id(), resume=False,
            model=self.secondary.spec.model, effort="low", workdir=request.workdir,
            readable_dirs=(), allowed_tools=(), trust_level=request.trust_level,
        )
        response = self.runner.run(self.secondary, probe_request, timeout_sec=self.PROBE_TIMEOUT_SEC)
        detail = response.body.strip() if response.body else ""
        return bool(response.ok and detail), (detail[:200] or "응답이 비어 있다")
