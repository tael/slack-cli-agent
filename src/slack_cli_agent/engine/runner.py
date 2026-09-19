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

import dataclasses
import logging
import os
import subprocess
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from ..auth.principal import TrustLevel
from ..config.settings import RuntimeSettings
from ..observability.progress_hook import append_tool
from ..session.manager import CAPABILITY_UNMET_REASON
from .base import (
    CallOrigin,
    ElapsedSource,
    Engine,
    EngineRequest,
    EngineResponse,
    FailureDetail,
    UsageLimit,
)
from .capability import (
    BOUNDARY_AXIS,
    ISOLATION_AXIS,
    TOOL_AXIS,
    EngineCapabilities,
    ExecutionIsolation,
    InstructionBoundary,
    ToolRestriction,
)
from .environment import EngineEnvironmentPolicy
from .stream import run_streaming
from .switcher import EngineSwitcher

SubprocessRunner = Callable[..., Any]

#: Audit kind for the capability record. Matches IncidentKind.CAPABILITY;
#: kept as a literal here so this module doesn't import observability.
log = logging.getLogger(__name__)

CAPABILITY_KIND = "capability"
#: Matches IncidentKind.PAYLOAD, kept as a literal for the same reason.
PAYLOAD_KIND = "payload"

#: For the notice the person who asked reads. Axis names and levels are
#: internal identifiers; putting them in a Slack reply tells nobody anything.
AXIS_NAMES = {
    TOOL_AXIS: "도구 제한",
    ISOLATION_AXIS: "실행 격리",
    BOUNDARY_AXIS: "지침 경계",
}

LEVEL_NAMES: dict[ToolRestriction | ExecutionIsolation | InstructionBoundary, str] = {
    ToolRestriction.NONE: "제한 없음",
    ToolRestriction.COARSE_SANDBOX: "샌드박스 수준",
    ToolRestriction.EXACT_ALLOWLIST: "허용된 도구 목록",
    ExecutionIsolation.NONE: "격리 없음",
    ExecutionIsolation.WORKSPACE_WRITE: "작업공간 쓰기",
    ExecutionIsolation.READONLY_SANDBOX: "읽기 전용 샌드박스",
    InstructionBoundary.UNAVAILABLE: "경계 없음",
    InstructionBoundary.PROMPT_ONLY: "프롬프트 안 표식뿐",
    InstructionBoundary.NATIVE: "엔진 자체 경계",
}


#: What this request's capability check came to.
OUTCOME_COMPATIBLE = "compatible"
OUTCOME_DOWNGRADED = "downgraded"
OUTCOME_BLOCKED = "blocked"


def _outcome(unmet: Sequence[str], downgraded: Sequence[str]) -> str:
    if not unmet:
        return OUTCOME_COMPATIBLE
    return OUTCOME_DOWNGRADED if downgraded else OUTCOME_BLOCKED


def _level(value: ToolRestriction | ExecutionIsolation | InstructionBoundary | None) -> str:
    if value is None:
        return "요구 없음"
    return LEVEL_NAMES.get(value, str(value))


class CapabilityAuditPort(Protocol):
    """AuditLog.record's shape. Optional -- a runner without one still runs.

    record() must raise when the record does not land. The runner grants an
    audited downgrade only on a call that returned, so a port that swallows
    its own failures turns that downgrade into an unrecorded one (sca-ckm).
    """

    def record(self, kind: str, *, channel: str = "", thread_ts: str = "", **fields: Any) -> None: ...


def _as_audit(capabilities: EngineCapabilities) -> dict[str, str]:
    return {
        "tool_restriction": str(capabilities.tool_restriction),
        "execution_isolation": str(capabilities.execution_isolation),
        "instruction_boundary": str(capabilities.instruction_boundary),
    }


class EngineRunner:
    """Runs an engine's command via subprocess and parses the result.

    The 900s timeout is measured: 300s cut off 2 of 150 requests, with
    a 34s median (RuntimeSettings.request_timeout_sec default).
    """

    def __init__(self, settings: RuntimeSettings,
               subprocess_runner: SubprocessRunner | None = None,
               environment_policy: EngineEnvironmentPolicy | None = None,
               source_env: Mapping[str, str] | None = None,
               audit: CapabilityAuditPort | None = None) -> None:
        self._settings = settings
        self._run = subprocess_runner or self._default_runner
        # Overrides the policy for every engine this runner runs. Normally left
        # unset: each Engine supplies its own, so a fallback turn runs under the
        # secondary's home rather than the primary's.
        self._environment_policy = environment_policy
        self._source_env = source_env
        # Recording only, for now. The engine that actually runs isn't known
        # until here, so the record names the real one on a fallback turn.
        self._audit = audit

    def run(self, engine: Engine, request: EngineRequest,
           timeout_sec: float | None = None) -> EngineResponse:
        # Resolved before prepare() so an engine with no policy fails before
        # writing its config file.
        policy = self._environment_policy or engine.environment_policy()
        if request.session_id is None:
            # Only here is the concrete engine known: FallbackEngine picks
            # primary or secondary in its own run(), and the recovery probe
            # sends a switched-state request back to the primary.
            request = dataclasses.replace(request, session_id=engine.new_session_id())
        if not request.model:
            # Same reason as session_id: model naming is per-engine and the
            # concrete engine is only known here (sca-dyb.10).
            request = dataclasses.replace(request, model=engine.spec.model)
        actual = engine.capabilities_for(request)
        recorded = self._record_capabilities(engine, request, actual)
        blocked = self._blocked_response(engine, request, actual, recorded)
        if blocked is not None:
            return blocked
        # After the block check: a refused request never reaches the process,
        # and counting it would put bytes nobody sent into the measurement.
        self._record_payload(engine, request)
        engine.prepare(request)
        cmd = engine.build_command(request)
        timeout = timeout_sec if timeout_sec is not None else self._settings.request_timeout_sec
        source = self._source_env if self._source_env is not None else os.environ
        extra: dict[str, Any] = {"env": policy.build(source)}
        sink = self._progress_sink(engine, request)
        if sink is not None:
            extra["on_stdout_line"] = sink
        started = time.monotonic()
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
                failure_detail=FailureDetail(timeout_sec=timeout_int),
                elapsed_source=ElapsedSource.RUNNER,
                engine=engine.name,
            )
        wall_elapsed = time.monotonic() - started
        # Stamped here rather than in each Engine.parse() so every engine reports
        # it the same way, including ones added later.
        response = dataclasses.replace(
            engine.parse(completed.stdout, completed.stderr, completed.returncode), engine=engine.name,
        )
        # sca-cfa — some engines (Codex) never report their own elapsed time.
        # Only fill in the runner's wall-clock measurement when the engine
        # left it unknown; an engine-reported value is more precise (it can
        # exclude time this process itself spent, e.g. queueing).
        if response.elapsed_source == ElapsedSource.UNKNOWN:
            response = dataclasses.replace(response, elapsed=wall_elapsed, elapsed_source=ElapsedSource.RUNNER)
        return response

    @staticmethod
    def _progress_sink(engine: Engine, request: EngineRequest) -> Callable[[str], None] | None:
        """Turns the engine's own stdout events into progress log lines.

        None when there is nothing to write to (progress off for this channel)
        or when the engine reports progress another way -- Claude's hook
        process writes the same file, and streaming as well would record
        every tool call twice (sca-8ks).
        """
        log_path = request.progress_log
        if log_path is None or not engine.streams_progress:
            return None

        def sink(line: str) -> None:
            append_tool(log_path, engine.progress_tool_name(line))

        return sink

    def _blocked_response(
        self, engine: Engine, request: EngineRequest, actual: EngineCapabilities, recorded: bool
    ) -> EngineResponse | None:
        """Refuses before prepare() when a declared guarantee is weaker than required.

        Every axis counts. Enforcing one of the three would tell the caller that
        setting the other two means something when it does not (sca-igu).
        """
        required = request.requirements
        unmet = required.unmet(actual)
        if not unmet:
            return None
        # The relief valve is an *audited* downgrade. With nowhere to record it,
        # granting it anyway would leave no trace that the guarantee was given
        # up, which is the one thing the name promises (sca-gpe).
        audit_missing = False
        record_failed = False
        if required.downgraded(unmet):
            if recorded:
                return None
            if self._audit is None:
                audit_missing = True
                log.warning("감사 기록기가 주입되지 않아 완화를 받아주지 않는다 : 엔진 %s", engine.name)
            else:
                record_failed = True
                log.warning("감사 기록에 실패해 완화를 받아주지 않는다 : 엔진 %s", engine.name)
        body = (
            "요청이 요구한 실행 보장을 이 엔진이 맞추지 못해 실행하지 않았습니다. "
            + " ".join(
                f"{AXIS_NAMES[axis]} 요구 {_level(getattr(required, axis))},"
                f" {engine.name} 보장 {_level(getattr(actual, axis))}."
                for axis in unmet
            )
        )
        if audit_missing:
            # Naming only the engine limit would read as an engine problem when
            # the direct condition is that this runner was built without an
            # audit recorder -- an assembly issue, not the engine's (sca-gpe).
            body += " 완화가 허용된 요청이지만 감사 기록기가 구성되지 않아 완화를 적용하지 않았습니다."
        if record_failed:
            body += " 완화가 허용된 요청이지만 감사 기록을 남기지 못해 완화를 적용하지 않았습니다."
        return EngineResponse(
            ok=False,
            body=body,
            session_id=request.session_id, model_actual=None,
            elapsed=0.0, turns=None, usage=None,
            raw={}, failure_reason=CAPABILITY_UNMET_REASON,
            # Written for the person who asked: without it they only see the
            # failure mark and can't tell this from a crash (sca-5sc).
            user_facing=True,
            # One axis, not a joined string: the audit reads this against an
            # allowlist and a joined value lands in it as unknown.
            failure_detail=FailureDetail(code=unmet[0]),
            elapsed_source=ElapsedSource.RUNNER,
            engine=engine.name,
        )

    def _record_payload(self, engine: Engine, request: EngineRequest) -> None:
        """What this call costs, apart from what it guarantees.

        Failing here must not take the request down: this is measurement, and
        a request that would have run fine should not die for a size record.
        """
        if self._audit is None:
            return
        try:
            self._audit.record(
                PAYLOAD_KIND,
                engine=engine.name,
                request_id=request.request_id,
                resume=request.resume,
                **engine.footprint_for(request).as_audit_dict(),
                # Absent rather than zeroed when the caller composed nothing:
                # "not budget-limited" and "never went through the composer"
                # have to stay apart in the audit.
                **dict(request.budget_report),
            )
        except Exception:
            log.warning("전송량 기록에 실패했다", exc_info=True)

    def _record_capabilities(
        self, engine: Engine, request: EngineRequest, actual: EngineCapabilities
    ) -> bool:
        """True only when this request's record actually landed.

        A failure here must not take down a request that met its guarantees --
        the record is the basis for a downgrade, not for the run itself.
        """
        if self._audit is None:
            return False
        required = request.requirements
        unmet = required.unmet(actual)
        downgraded = required.downgraded(unmet)
        record_fields: dict[str, Any] = {
            "engine": engine.name,
            # Always present, empty when the caller set none: "no key" and "the
            # field was never written" have to stay apart in the audit.
            "request_id": request.request_id,
            "required": {
                axis: str(value)
                for axis in ("tool_restriction", "execution_isolation", "instruction_boundary")
                if (value := getattr(required, axis)) is not None
            },
            "actual": _as_audit(actual),
            "unmet": list(unmet),
            "downgradable_axes": sorted(required.downgradable_axes),
            "policy": required.policy,
            # Permitted and authorized are different questions. Counting
            # relieved requests needs the second one (sca-98k). Only axes the
            # caller named are counted, so a tool-only policy can't read as
            # having relieved isolation too.
            "downgraded_axes": list(downgraded),
            # Named for what this record can know. It is written before the run,
            # so a later prepare() or process failure leaves it standing: it
            # says the downgrade was authorized, not that the request finished
            # under it (sca-ckm).
            "downgrade_authorized": bool(downgraded),
            # One value for the counting side. Deriving it from unmet and the
            # downgrade list means every reader rewrites that rule (sca-98k).
            # Same time frame as downgrade_authorized -- this is the capability
            # decision, not how the request ended.
            "outcome": _outcome(unmet, downgraded),
        }
        try:
            self._audit.record(CAPABILITY_KIND, **record_fields)
        except Exception:
            # exc_info: a port bug and a disk failure both land here and the
            # message alone does not separate them.
            log.warning("보장 감사 기록에 실패했다", exc_info=True)
            return False
        return True

    @staticmethod
    def _default_runner(
        cmd: list[str], cwd: str, timeout: float, env: Mapping[str, str] | None = None,
        on_stdout_line: Callable[[str], None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if on_stdout_line is not None:
            return run_streaming(cmd, cwd, timeout, env, on_stdout_line)
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
    def invoke(self, request: EngineRequest,
               origin: CallOrigin = CallOrigin.INTERACTIVE) -> EngineResponse: ...


class EngineRunPort(Protocol):
    """What DirectInvoker needs. EngineRunner satisfies it."""

    def run(self, engine: Engine, request: EngineRequest) -> EngineResponse: ...


class DirectInvoker(EngineInvoker):
    """No fallback configured — runs that one engine via the runner."""

    def __init__(self, runner: EngineRunPort, engine: Engine) -> None:
        self._runner = runner
        self._engine = engine

    def invoke(self, request: EngineRequest,
               origin: CallOrigin = CallOrigin.INTERACTIVE) -> EngineResponse:
        # No fallback means no recovery probe, so origin has nothing to gate.
        return self._runner.run(self._engine, request)


class FallbackInvoker(EngineInvoker):
    """Fallback configured — delegates to FallbackEngine.run(), which includes the switch decision."""

    def __init__(self, engine: FallbackEngine) -> None:
        self._engine = engine

    def invoke(self, request: EngineRequest,
               origin: CallOrigin = CallOrigin.INTERACTIVE) -> EngineResponse:
        return self._engine.run(request, origin)


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
        return self._active.new_session_id()

    def detect_usage_limit(self, response: EngineResponse) -> UsageLimit | None:
        return self._active.detect_usage_limit(response)

    def session_id_from(self, response: EngineResponse) -> str | None:
        return self._active.session_id_from(response)

    def directives_for_turn(self, request: EngineRequest) -> str:
        return self._active.directives_for_turn(request)

    def environment_policy(self) -> EngineEnvironmentPolicy:
        # This class has no profile block of its own; the policy belongs to
        # whichever engine is actually running this turn.
        return self._active.environment_policy()

    def readable_paths_note(self, paths: Sequence[Path]) -> str:
        return self._active.readable_paths_note(paths)

    def capabilities_for(self, request: EngineRequest) -> EngineCapabilities:
        # A fallback turn runs the secondary. Reporting this class's own
        # default would make every capability record on that turn false.
        return self._active.capabilities_for(request)

    # -- The real entry point.
    def run(self, request: EngineRequest,
            origin: CallOrigin = CallOrigin.INTERACTIVE) -> EngineResponse:
        """Checks switch state and runs on primary or secondary accordingly."""
        state = self.switcher.load()

        if state:
            if origin is CallOrigin.INTERACTIVE and self.switcher.should_probe(time.time()):
                recovered = self._probe_primary_recovery(request)
                if recovered is not None:
                    return recovered

            if not self.switcher.is_approved():
                self._active = self.primary
                approval = str(self.switcher.load().get("approval", "pending"))
                return EngineResponse(
                    ok=False, body=self.switcher.limit_reply(), session_id=None,
                    model_actual=None, elapsed=0.0, turns=None, usage=None,
                    user_facing=True,
                    raw={"engine_switch": approval},
                    failure_reason="usage_limit", engine=self.primary.name,
                    failure_detail=FailureDetail(code=approval),
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
        secondary's own configured model -- the owner grade included, or
        the owner's request would quietly drop to the secondary's general
        model (sca-14h). A channel's explicitly named model can't carry
        over at all; engine model names don't correspond.
        """
        self._active = self.secondary
        owner = request.trust_level is TrustLevel.OWNER
        model = self.secondary.spec.model_for_owner() if owner else None
        fallback_request = EngineRequest(
            prompt=request.prompt, system_prompt=request.system_prompt,
            session_id=None, resume=False,
            model=model, effort=request.effort,
            workdir=request.workdir, readable_dirs=request.readable_dirs,
            allowed_tools=request.allowed_tools, trust_level=request.trust_level,
            # The boundary the caller asked for does not stop applying because
            # the primary ran out of quota. Dropping it turned a rate limit into
            # a permission bypass (sca-93u).
            requirements=request.requirements,
            # Same request with the same person waiting on it. Dropping this
            # would leave the already-open progress display stuck on its
            # opening line for the whole fallback turn.
            progress_log=request.progress_log,
            # The same reason in the audit: this is the next attempt at one
            # request, not a second request (sca-4ol).
            request_id=request.request_id,
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
            session_id=None, resume=False,
            model=None, effort="low", workdir=request.workdir,
            readable_dirs=(), allowed_tools=(), trust_level=request.trust_level,
            request_id=request.request_id,
        )
        response = self.runner.run(self.secondary, probe_request, timeout_sec=self.PROBE_TIMEOUT_SEC)
        detail = response.body.strip() if response.body else ""
        return bool(response.ok and detail), (
            (detail[:200] or "응답이 비어 있다") + self._capability_note(request)
        )

    def _capability_note(self, request: EngineRequest) -> str:
        """The probe asks whether the secondary answers at all. Approving on
        that alone reads as 'this engine can take over', which is a different
        question from whether it holds this request's guarantees (sca-42s)."""
        required = request.requirements
        unmet = required.unmet(self.secondary.capabilities_for(request))
        if not unmet:
            return ""
        axes = ", ".join(AXIS_NAMES[axis] for axis in unmet)
        if required.downgraded(unmet):
            return f" 다만 {axes} 은 이 엔진이 보장하지 못해 완화 기록을 남기고 실행한다."
        return f" 다만 {axes} 을 요구한 요청은 이 엔진에서 실행되지 않는다."
