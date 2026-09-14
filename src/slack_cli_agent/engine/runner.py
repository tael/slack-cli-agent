"""Wraps engine execution and fallback switching.

EngineRunner only runs the subprocess. Command assembly
(build_command) and output parsing (parse) are pure functions Engine
provides, so this layer is just the execution step between them,
letting tests inject a subprocess double.

FallbackEngine wraps EngineRunner to handle primary/secondary
execution and the switch decision. Callers only see this one class —
they don't need to know whether a switch happened.
"""

from __future__ import annotations

import os
import subprocess
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..config.settings import RuntimeSettings
from .base import Engine, EngineRequest, EngineResponse, UsageLimit
from .environment import EngineEnvironmentPolicy
from .switcher import EngineSwitcher

SubprocessRunner = Callable[..., Any]


class EngineRunner:
    """Runs an engine's command via subprocess and parses the result.

    The 900s timeout is measured: 300s cut off 2 of 150 requests, with
    a 34s median (RuntimeSettings.request_timeout_sec default).
    """

    def __init__(self, settings: RuntimeSettings,
               subprocess_runner: SubprocessRunner | None = None,
               environment_policy: EngineEnvironmentPolicy | None = None,
               source_env: Mapping[str, str] | None = None) -> None:
        self._settings = settings
        self._run = subprocess_runner or self._default_runner
        # Environment isolation policy. If not given, no env argument
        # is passed at all and the child inherits the parent's — kept
        # for existing callers that don't provide one yet. Passing an
        # empty environment would break the engine by hiding PATH.
        self._environment_policy = environment_policy
        self._source_env = source_env

    def run(self, engine: Engine, request: EngineRequest,
           timeout_sec: float | None = None) -> EngineResponse:
        cmd = engine.build_command(request)
        timeout = timeout_sec if timeout_sec is not None else self._settings.request_timeout_sec
        # Skip the env argument entirely when there's no policy —
        # passing it would break existing callers whose injected
        # runner double doesn't accept that kwarg.
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
    def _default_runner(
        cmd: list[str], cwd: str, timeout: float, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        # engine.parse() interprets returncode directly — turning it
        # into an exception here would break the path that carries a
        # failed exit code as a failure response.
        return subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            env=dict(env) if env is not None else None, check=False,
        )


class EngineInvoker(ABC):
    """Runs one engine call. Callers don't know whether fallback is configured.

    Calling EngineRunner.run(engine, request) directly, bypassing this,
    skips FallbackEngine.run() even when fallback is configured — its
    switch decision and state recording never run, so hitting a usage
    limit never triggers a switch. Assembly decides which invoker to
    use; callers only see this contract.
    """

    @abstractmethod
    def invoke(self, request: EngineRequest) -> EngineResponse: ...


class DirectInvoker(EngineInvoker):
    """No fallback configured — runs that one engine via the runner."""

    def __init__(self, runner: EngineRunner, engine: Engine) -> None:
        self._runner = runner
        self._engine = engine

    def invoke(self, request: EngineRequest) -> EngineResponse:
        return self._runner.run(self._engine, request)


class FallbackInvoker(EngineInvoker):
    """Fallback configured — delegates to FallbackEngine.run(), which includes the switch decision."""

    def __init__(self, engine: FallbackEngine) -> None:
        self._engine = engine

    def invoke(self, request: EngineRequest) -> EngineResponse:
        return self._engine.run(request)


class FallbackEngine(Engine):
    """Delegates to the secondary engine when the primary reports a usage limit.

    Callers only see one Engine and don't need to know whether a
    switch happened. Same behavior as the original bot.py's
    run_with_fallback() — switching happens immediately, but the bot
    only replies with a limit notice until a human approves it.
    EngineSwitcher manages engine_state.json; this class handles the
    actual execution branch.
    """

    name = "fallback"

    # Short request confirming the fallback engine is actually usable.
    # Same as the original ENGINE_PROBE_PROMPT.
    PROBE_PROMPT = "준비됐으면 OK 두 글자만 답해라."
    # Timeout for the probe only. Same as the original
    # ENGINE_PROBE_TIMEOUT. Using the full request_timeout_sec here
    # would make a human wait just as long when the fallback engine is
    # unresponsive.
    PROBE_TIMEOUT_SEC = 120.0

    def __init__(self, primary: Engine, secondary: Engine, switcher: EngineSwitcher,
               runner: EngineRunner) -> None:
        super().__init__(primary.profile, primary.settings)
        self.primary = primary
        self.secondary = secondary
        self.switcher = switcher
        self.runner = runner
        # Delegate target for callers that invoke build_command/parse
        # directly on this class. run() decides which engine is
        # actually active and updates this.
        self._active: Engine = primary

    # -- Engine contract. Delegates for callers that use build_command
    # /parse directly on this class instead of going through
    # EngineRunner. Real callers use run().
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

    def readable_paths_note(self, paths: Sequence[Path]) -> str:
        return self._active.readable_paths_note(paths)

    # -- The real entry point.
    def run(self, request: EngineRequest) -> EngineResponse:
        """Checks switch state and runs on primary or secondary accordingly."""
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
        """Handles this turn on the approved fallback engine.

        A different engine can't continue a session, so this opens a
        new one. Model naming also differs per engine, so it uses the
        secondary's own configured model.
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
        """Probes whether the primary has recovered, using a real request.

        Only happens on the request path — a human is waiting there,
        so they should be the first to benefit once it's recovered,
        and this turn can be handled immediately.
        """
        self._active = self.primary
        response = self.runner.run(self.primary, request)
        self.switcher.mark_probed(time.time())
        if response.ok and self.primary.detect_usage_limit(response) is None:
            self.switcher.recover()
            return response
        return None

    def _probe_secondary(self, request: EngineRequest) -> tuple[bool, str]:
        """Confirms the fallback engine actually answers, via a short request.

        Flipping the state file and declaring a switch without this
        risks a human trusting a switch to an engine that doesn't
        actually work either.
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
