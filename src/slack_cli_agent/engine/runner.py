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
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from ..config.settings import RuntimeSettings
from ..core.errors import ConfigError
from ..core.secrets import redact
from ..core.timezones import KST
from ..observability.progress_hook import append_tool
from ..session.manager import AUTH_FAILURE_REASON, CAPABILITY_UNMET_REASON
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
from .environment import EngineEnvironmentPolicy, turn_budget_env
from .stream import run_streaming
from .switcher import EngineSwitcher

SubprocessRunner = Callable[..., Any]

#: Audit kind for the capability record. Matches IncidentKind.CAPABILITY;
#: kept as a literal here so this module doesn't import observability.
log = logging.getLogger(__name__)

CAPABILITY_KIND = "capability"
#: Matches IncidentKind.PAYLOAD, kept as a literal for the same reason.
PAYLOAD_KIND = "payload"

#: Failure reason for a turn whose command could not be assembled. Its own
#: name so the audit does not count a settings fault as a CLI crash, and so
#: the new-session retry does not run it again at the same disadvantage.
ENGINE_CONFIG_REASON = "engine_config"

#: How much of a timed-out turn's output to keep. The whole stream can be
#: huge; the tail holds what the engine was doing when it stopped, which is
#: what a human reads to find the cause (B11, bot.py 의 800자 꼬리와 같은 폭).
TAIL_CHARS = 800

#: 모델 폴백 재시도에 최소로 필요한 남은 시간(초). 이보다 적게 남았으면 다음
#: 모델을 돌리지 않는다 - 시작하자마자 또 끊겨 슬롯만 축낸다 (B17).
MODEL_RETRY_MIN_SEC = 60


#: How much of a nonzero-exit run's output to keep. A CLI that exits early
#: writes why at the top - usage, a config error, a stack trace - so the head
#: is what a human reads, not the tail (C8, bot.py 의 stdout 앞 2000자와 같은 폭).
HEAD_CHARS = 2000


def head_chars(text: str | None, limit: int = HEAD_CHARS) -> str:
    """First `limit` characters of `text`; empty for None/empty."""
    if not text:
        return ""
    return text[:limit]


def tail_chars(text: str | None, limit: int = TAIL_CHARS) -> str:
    """Last `limit` characters of `text`; empty for None/empty."""
    if not text:
        return ""
    return text[-limit:]


def _as_text(raw: str | bytes | None) -> str:
    """TimeoutExpired carries str under text=True, but tolerate bytes too."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return raw

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
    ToolRestriction.SERVER_SCOPED_ALLOWLIST: "허용 목록, MCP 는 서버 단위",
    ToolRestriction.EXACT_ALLOWLIST: "허용된 도구 목록",
    ToolRestriction.ALL_FORBIDDEN: "도구 전부 금지",
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


#: TURN_ID for a call made outside a Slack turn, same as mametchi's
#: MAMETCHI_TURN=unknown (bot.py), which restart.ApplyRequest reads as no turn.
UNKNOWN_TURN_ID = "unknown"


def _with_default_turn_id(request: EngineRequest) -> EngineRequest:
    """Give a request without a turn key one, so helper wrappers and MCP
    servers still record which call ran them (C10).

    Watch, review and learning calls build their EngineRequest without the
    pipeline's TURN_ID; their audit rows then read the same empty turn as a
    person's terminal call. The request_id (watch-<id>-<run_id> and the like)
    names the call; "unknown" is left for a request with none. A caller's own
    TURN_ID or turn_context turn_id is never replaced."""
    turn = request.engine_env.get("TURN_ID") or request.turn_context.get("turn_id")
    if not turn:
        turn = request.request_id or UNKNOWN_TURN_ID
    engine_env = request.engine_env
    turn_context = request.turn_context
    if not engine_env.get("TURN_ID"):
        engine_env = {**engine_env, "TURN_ID": turn}
    if not turn_context.get("turn_id"):
        turn_context = {**turn_context, "turn_id": turn}
    return dataclasses.replace(request, engine_env=engine_env, turn_context=turn_context)


def _outcome(unmet: Sequence[str], downgraded: Sequence[str]) -> str:
    if not unmet:
        return OUTCOME_COMPATIBLE
    return OUTCOME_DOWNGRADED if downgraded else OUTCOME_BLOCKED


def _level(value: ToolRestriction | ExecutionIsolation | InstructionBoundary | None) -> str:
    if value is None:
        return "요구 없음"
    return LEVEL_NAMES.get(value, str(value))


def _switch_time(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, KST).strftime("%m-%d %H:%M")


def switch_dm_body(*, bot_name: str, from_engine: str, to_engine: str,
                   reason: str, detail: str, probe_ok: bool,
                   probe_detail: str, switched_at: float,
                   approved: bool = False) -> str:
    """전환을 시작했음을 소유자에게 알리는 DM 본문 (B24).

    사유·시각·확인결과를 담는다. 마메치 bot.py 의 engine_switch_notice 와
    같은 구성이되, 한도 소진과 로그인 만료 두 계기를 구분한다 - 사람이 할 일이
    다르기 때문이다(한도는 기다리면 풀리고 로그인은 다시 로그인해야 한다).
    승인 전에는 전환 사유와 똑같이 답하므로, 바로 쓰려면 승인/거부를 안내한다.
    """
    cause = "로그인 만료" if reason == AUTH_FAILURE_REASON else "구독 한도 소진"
    lines = [
        f"*{bot_name} 실행기를 {to_engine} 로 바꿨습니다*",
        "",
        f"- 사유 : {from_engine} {cause}",
        f"- 시각 : {_switch_time(switched_at)} KST",
    ]
    detail = (detail or "").strip()
    if detail:
        lines.append(f"- 원문 : {detail}")
    if probe_ok and approved:
        lines += [
            f"- 전환 확인 : {to_engine} 가 응답합니다",
            "",
            f"이 요청부터 {to_engine} 로 답합니다. {from_engine} 가 돌아오면 자동으로 되돌립니다.",
        ]
        if reason == AUTH_FAILURE_REASON:
            # 로그인 만료는 기다려도 풀리지 않는다. 사람이 할 일을 적는다.
            lines.append(f"{from_engine} 는 다시 로그인하거나 토큰을 다시 발급해야 돌아옵니다.")
        lines.append("쓰지 않으려면 `엔진 거부` 를 보내주세요.")
    elif probe_ok:
        lines += [
            f"- 전환 확인 : {to_engine} 가 응답합니다",
            "",
            "지금은 승인 전이라 전환 사유와 똑같이 답합니다.",
            f"{to_engine} 로 답하게 하려면 이 대화에서 `엔진 승인` 을,",
            "쓰지 않으려면 `엔진 거부` 를 보내주세요.",
        ]
    else:
        lines += [
            f"- 전환 확인 실패 : {probe_detail}",
            "",
            f"{to_engine} 도 지금은 쓸 수 없습니다. 복구될 때까지 기다려야 합니다.",
        ]
    return "\n".join(lines)


def recovery_dm_body(*, bot_name: str, primary_engine: str, recovered_at: float) -> str:
    """기본 실행기가 돌아와 되돌렸음을 알리는 DM 본문 (B26)."""
    return (
        f"*{bot_name} 실행기를 {primary_engine} 로 되돌렸습니다*\n\n"
        f"- {primary_engine} 가 정상 복귀해 원래 실행기로 답합니다\n"
        f"- 시각 : {_switch_time(recovered_at)} KST"
    )


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
        try:
            # Resolved before prepare() so an engine with no policy fails before
            # writing its config file. engine.spec (for the model name below)
            # and capabilities_for() can raise the same way, so they share this
            # try -- none of the three ever reached the block below uncaught
            # (코덱스 6차 리뷰 결함3).
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
        except ConfigError as e:
            return self._config_error_response(engine, request, e)
        recorded = self._record_capabilities(engine, request, actual)
        blocked = self._blocked_response(engine, request, actual, recorded)
        if blocked is not None:
            return blocked
        # After the block check: a refused request never reaches the process,
        # and counting it would put bytes nobody sent into the measurement.
        timeout = timeout_sec if timeout_sec is not None else self._settings.request_timeout_sec
        # Set before build_command: an engine that passes its own limit to the
        # CLI has to name the one enforced here (sca-ocie).
        request = dataclasses.replace(request, timeout_sec=timeout)
        request = _with_default_turn_id(request)
        self._record_payload(engine, request)
        base_source = self._source_env if self._source_env is not None else os.environ
        # Stamp the turn deadline so the child engine's Stop hook and Bash
        # PreToolUse guard can see how much turn budget is left (A28). Computed
        # once per turn from the enforced timeout; the env policy's allowlist
        # carries TURN_DEADLINE[_MONO] through to the child. The caller's
        # per-turn facts (request.engine_env: current channel, advisor and
        # code-first channel sets) go on the same way; the policy still decides
        # which names pass.
        source = {
            **base_source,
            **request.engine_env,
            **turn_budget_env(timeout, now_wall=time.time(), now_monotonic=time.monotonic()),
        }

        # B9/B17 — 모델 하나의 일시 장애면 같은 엔진 안에서 폴백 모델로 순차
        # 재시도한다. 폴백 목록이 없으면(기본) candidates 가 요청 모델 하나뿐이라
        # 루프가 한 번만 돌아 예전과 같은 동작이다.
        candidates = self._model_candidates(engine, request)
        loop_started = time.monotonic()
        last: EngineResponse | None = None
        for index, model in enumerate(candidates):
            is_last = index == len(candidates) - 1
            if index == 0:
                attempt_timeout = timeout
            else:
                # 폴백 시도는 남은 시간 안에서만 돈다. 시도마다 처음 상한을 새로
                # 주지 않는다 - 한 요청이 슬롯을 끝없이 쥐지 않게 한다.
                remaining = timeout - (time.monotonic() - loop_started)
                if remaining < MODEL_RETRY_MIN_SEC:
                    break
                attempt_timeout = remaining
            attempt = dataclasses.replace(request, model=model, timeout_sec=attempt_timeout)
            try:
                engine.prepare(attempt)
                cmd = engine.build_command(attempt)
            except ConfigError as e:
                return self._config_error_response(engine, attempt, e)
            extra: dict[str, Any] = {"env": policy.build(source)}
            sink = self._progress_sink(engine, attempt)
            if sink is not None:
                extra["on_stdout_line"] = sink
            started = time.monotonic()
            try:
                completed = self._run(
                    cmd, cwd=str(attempt.workdir), timeout=attempt_timeout, **extra,
                )
            except subprocess.TimeoutExpired as expired:
                # 타임아웃은 모델 장애 폴백 계기가 아니다 - 다른 모델도 비슷한
                # 시간이 걸릴 수 있고 남은 시간은 더 짧다. 그대로 중단 응답을 낸다.
                return self._timeout_response(engine, attempt, expired, attempt_timeout)
            wall_elapsed = time.monotonic() - started
            # Stamped here rather than in each Engine.parse() so every engine
            # reports it the same way, including ones added later.
            response = dataclasses.replace(
                engine.parse(completed.stdout, completed.stderr, completed.returncode),
                engine=engine.name,
                model_asked=attempt.model or "",
            )
            # sca-cfa — some engines (Codex) never report their own elapsed time.
            # Only fill in the runner's wall-clock measurement when the engine
            # left it unknown; an engine-reported value is more precise (it can
            # exclude time this process itself spent, e.g. queueing).
            if response.elapsed_source == ElapsedSource.UNKNOWN:
                response = dataclasses.replace(
                    response, elapsed=wall_elapsed, elapsed_source=ElapsedSource.RUNNER
                )
            last = response
            if completed.returncode != 0:
                # 종료코드만으로는 왜 실패했는지 알 수 없다. 원문은 로그-세이프
                # 경계를 지켜 FailureDetail 엔 넣지 않고 앞 2KB 만 로그로 남긴다 (C8).
                log.warning(
                    "엔진 %s 가 종료코드 %d 로 끝났습니다 (%s). 출력(앞 %d자)"
                    " stdout=%r stderr=%r",
                    engine.name, completed.returncode,
                    response.failure_reason or "실패 사유 없음", HEAD_CHARS,
                    redact(head_chars(_as_text(completed.stdout))),
                    redact(head_chars(_as_text(completed.stderr))),
                )
            # C15 — 성공한 턴이 세션에 실은 지침을 적어 둔다. 다음 재개 턴은
            # 그중 바뀌지 않은 블록을 다시 싣지 않는다.
            engine.note_delivered(attempt, response)
            # 모델 하나의 장애이고, 도구가 안 돌았고, 다음 모델이 남았으면 넘어간다.
            # 도구가 이미 돈 턴을 다시 돌리면 같은 쓰기가 두 번 난다.
            if (not response.ok and not is_last
                    and engine.detect_model_outage(completed.stdout, completed.stderr)
                    and not engine.tools_already_ran(completed.stdout)):
                log.warning(
                    "엔진 %s 모델 장애, 다음 모델로 넘어갑니다 : %s -> %s",
                    engine.name, model, candidates[index + 1],
                )
                continue
            return response
        # 폴백을 돌 시간이 모자라 멈춘 경우 - 마지막 실패를 그대로 돌려준다.
        assert last is not None  # candidates 는 최소 요청 모델 하나라 늘 채워진다
        return last

    @staticmethod
    def _model_candidates(engine: Engine, request: EngineRequest) -> list[str]:
        """이번 턴에 순서대로 시도할 모델들. 첫 모델은 요청 모델, 이어서 겹치지
        않는 폴백 모델들이다. 폴백이 없으면 요청 모델 하나뿐이다 (B17)."""
        primary = request.model or ""
        try:
            spec = engine.spec
        except ConfigError:
            return [primary]
        # getattr 기본값으로 둔다 - model_fallbacks 가 없는 엔진 구현이나 대역도
        # 폴백 없이 요청 모델 하나로만 돈다.
        fallbacks = getattr(spec, "model_fallbacks", ())
        candidates = [primary]
        for model in fallbacks:
            if model and model != primary and model not in candidates:
                candidates.append(model)
        return candidates

    @staticmethod
    def _timeout_response(
        engine: Engine, request: EngineRequest,
        expired: subprocess.TimeoutExpired, timeout: float,
    ) -> EngineResponse:
        timeout_int = int(timeout)
        # 중단될 때 받은 출력은 '무엇을 하다 멈췄나'의 유일한 단서다.
        # 원문은 로그-세이프 경계를 지켜 FailureDetail 엔 넣지 않고(수만 센다)
        # 꼬리 800자만 로그로 남긴다 (B11).
        out_text = _as_text(expired.stdout)
        err_text = _as_text(expired.stderr)
        if out_text or err_text:
            log.warning(
                "엔진 %s 가 %d초 안에 안 끝나 중단했습니다. 마지막 출력(꼬리 %d자)"
                " stdout=%r stderr=%r",
                engine.name, timeout_int, TAIL_CHARS,
                tail_chars(out_text), tail_chars(err_text),
            )
        stdout_chars = len(out_text) if out_text else None
        return EngineResponse(
            ok=False,
            body=f"시간이 오래 걸려 중단했습니다. {timeout_int}초 안에 끝나지 않았습니다.",
            session_id=None, model_actual=None,
            elapsed=timeout, turns=None, usage=None,
            raw={}, failure_reason="timeout",
            failure_detail=FailureDetail(
                timeout_sec=timeout_int, stdout_chars=stdout_chars
            ),
            elapsed_source=ElapsedSource.RUNNER,
            engine=engine.name,
            model_asked=request.model or "",
        )

    @staticmethod
    def _config_error_response(
        engine: Engine, request: EngineRequest, error: ConfigError
    ) -> EngineResponse:
        """A turn that could not be assembled, as a response rather than a raise.

        Command assembly reads the settings files, and a missing deny list
        raises there. Letting it out of run() skipped everything that acts on
        a response -- the reply the asker gets, the failure reason the audit
        records, and FallbackEngine's switch decision, which only reads
        responses (sca-z1et).

        Not a switch trigger. Switching is persistent and waits for a human
        to approve it, and its two triggers -- usage limit, login expiry --
        are states the primary cannot leave on its own. A settings fault is
        the operator's to fix now, and moving the bot onto the secondary
        would park it there while hiding the fault that sent it. The deny
        list also exists to hold this turn down; answering its loss by
        running the turn somewhere else is the opposite of what sca-1aji
        chose.
        """
        return EngineResponse(
            ok=False,
            body=f"엔진 설정을 읽지 못해 실행하지 않았습니다. {error}",
            session_id=request.session_id, model_actual=None,
            elapsed=0.0, turns=None, usage=None,
            raw={}, failure_reason=ENGINE_CONFIG_REASON,
            # Without it the asker sees only the failure mark and cannot tell
            # this from a crash, and a settings fault needs an operator.
            user_facing=True,
            elapsed_source=ElapsedSource.RUNNER,
            engine=engine.name,
            model_asked=request.model or "",
        )

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
            model_asked=request.model or "",
        )

    def _record_payload(self, engine: Engine, request: EngineRequest) -> None:
        """What this call costs, apart from what it guarantees.

        Failing here must not take the request down: this is measurement, and
        a request that would have run fine should not die for a size record.
        """
        if self._audit is None:
            return
        try:
            tier = {"tier": request.turn_tier} if request.turn_tier else {}
            self._audit.record(
                PAYLOAD_KIND,
                engine=engine.name,
                request_id=request.request_id,
                resume=request.resume,
                **tier,
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
        if request.turn_tier:
            record_fields["tier"] = request.turn_tier
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
            # Same reason as run_streaming: an inherited stdin can hang the CLI.
            stdin=subprocess.DEVNULL,
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


#: 단일 엔진에서 로그인이 풀렸을 때 스레드에 남기는 한 줄.
DIRECT_AUTH_FAILURE_REPLY = "엔진 로그인이 풀려 이 요청을 처리하지 못했습니다. 관리자에게 알렸습니다."

#: 로그인이 풀린 동안 들어오는 요청마다 DM 이 나가지 않게 막는 간격.
DIRECT_AUTH_NOTIFY_INTERVAL_SEC = 3600.0


class DirectInvoker(EngineInvoker):
    """No fallback configured — runs that one engine via the runner.

    로그인이 풀린 실패는 여기서 사람에게 보이게 바꾼다. 폴백이 없으면
    FallbackEngine 의 인증 실패 감지가 돌지 않아, codex 의 종료코드 1 이
    비공개 실패로 남고 스레드에는 아무 답도 없었다. 2026-10-20 데이전트가
    갱신 토큰 무효로 17:46 요청에 침묵한 사례다.
    """

    def __init__(self, runner: EngineRunPort, engine: Engine,
                 notify: Callable[[str], object] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._runner = runner
        self._engine = engine
        self._notify = notify
        self._clock = clock
        self._last_auth_notice = float("-inf")

    def invoke(self, request: EngineRequest,
               origin: CallOrigin = CallOrigin.INTERACTIVE) -> EngineResponse:
        # No fallback means no recovery probe, so origin has nothing to gate.
        response = self._runner.run(self._engine, request)
        # 성공은 판정할 것이 없다. 실행기 대역이 응답 아닌 값을 돌려주는 시험도
        # 그대로 통과시킨다.
        if not isinstance(response, EngineResponse) or response.ok:
            return response
        note = self._engine.detect_auth_failure(response)
        if note is None:
            return response
        self._notify_auth_failure(note)
        # auth_failure 는 NO_RETRY_REASONS 에 있어 새 세션 재시도로 같은
        # 자격을 한 번 더 태우지 않는다.
        return dataclasses.replace(
            response, body=DIRECT_AUTH_FAILURE_REPLY, user_facing=True,
            failure_reason=AUTH_FAILURE_REASON,
        )

    def _notify_auth_failure(self, note: str) -> None:
        if self._notify is None:
            return
        now = self._clock()
        if now - self._last_auth_notice < DIRECT_AUTH_NOTIFY_INTERVAL_SEC:
            return
        self._last_auth_notice = now
        try:
            self._notify(f"*{self._engine.name} 로그인이 풀렸습니다*\n\n- {note}")
        except Exception:  # 알림 실패가 요청 처리를 끝내면 안 된다
            log.exception("로그인 풀림 알림에 실패했다")


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
               runner: EngineRunner,
               notify: Callable[[str], object] | None = None,
               clock: Callable[[], float] = time.time) -> None:
        super().__init__(primary.profile, primary.settings)
        self.primary = primary
        self.secondary = secondary
        self.switcher = switcher
        self.runner = runner
        # 전환·복귀를 소유자 DM 으로 알린다. 없으면(소유자 미설정) 발송만 생략하고
        # 전환 자체는 그대로 일어난다. 알림 실패가 요청 처리를 끝내선 안 된다.
        self._notify = notify
        self._clock = clock
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

    def environment_policy(self) -> EngineEnvironmentPolicy:
        # This class has no profile block of its own; the policy belongs to
        # whichever engine is actually running this turn.
        return self._active.environment_policy()

    def readable_paths_note(self, paths: Sequence[Path]) -> str:
        return self._active.readable_paths_note(paths)

    def capabilities_for(self, request: EngineRequest) -> EngineCapabilities:
        # Asked before run() (pipeline.turn_reach), so it answers for the
        # engine run() will pick from the switch state, not the one the last
        # turn left in _active: that was stale at boot and after another
        # process switched. Reporting this class's own default would make
        # every capability record on a fallback turn false.
        return self._planned().capabilities_for(request)

    def resumes_with_opening_instructions(self) -> bool:
        # The engine this turn will run on, for the same reason as
        # capabilities_for above.
        return self._planned().resumes_with_opening_instructions()

    def _planned(self) -> Engine:
        if self.switcher.load() and self.switcher.is_approved():
            return self.secondary
        return self.primary

    @staticmethod
    def _fit(engine: Engine, request: EngineRequest) -> EngineRequest:
        """The request as the engine about to run it must see it.

        A turn can change engine after its prompt was written: a usage limit
        mid-turn, a recovery probe, a failed probe falling to the secondary.
        The fallback engine is always a different kind (profile.validate), so
        the other engine's sandbox would otherwise be described (A12 review).
        """
        if request.retarget is None:
            return request
        try:
            return request.retarget(request, engine.capabilities_for(request))
        except Exception:
            # The runner raises the same capability error on the run itself;
            # failing here would only hide it behind this one.
            log.warning("실행 엔진에 맞춰 요청을 고치지 못했다", exc_info=True)
            return request

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
                    # 무엇 때문에 전환했는지가 실패 기록에 남아야 한다. 인증
                    # 실패까지 usage_limit 으로 세면 한도 통계가 거짓이 된다.
                    failure_reason=self.switcher.reason(), engine=self.primary.name,
                    failure_detail=FailureDetail(code=approval),
                    model_asked=request.model or "",
                )

            return self._run_secondary(request)

        return self._run_primary(request)

    def _run_primary(self, request: EngineRequest) -> EngineResponse:
        """1차로 돌리고, 다음 요청도 똑같이 실패할 상태면 전환을 시작한다.

        전환 계기는 둘이다. 사용량 한도와 로그인 만료다. 둘 다 그 요청 하나의
        문제가 아니라 1차 엔진이 당분간 아무 요청도 처리하지 못하는 상태라서
        2차로 넘긴다. 일반적인 실패(타임아웃, 형식 오류, 종료코드 1)는 계기가
        아니다 - 그것까지 넣으면 한 번 끊긴 것으로 엔진이 바뀐다.
        """
        self._active = self.primary
        response = self.runner.run(self.primary, self._fit(self.primary, request))
        limit = self.primary.detect_usage_limit(response)
        if limit is not None:
            # 한도는 승인을 기다릴 일이 아니다 (2026-10-25, 2026-11-06 김태일 지시).
            # 2차가 확인 요청에 답하면 바로 승인하고, 한도를 만난 이 턴부터
            # 2차로 답한다. 2차도 답하지 않을 때만 안내문을 낸다.
            state = self._begin_switch(request, EngineSwitcher.USAGE_LIMIT, limit.detail,
                                       auto_approve=True)
            if state.get("approval") == "approved":
                return self._run_secondary(request)
            return self._announce_switch(response)
        auth_failure = self.primary.detect_auth_failure(response)
        if auth_failure is not None:
            # 로그인 만료도 한도와 같이 바로 2차로 답한다 (2026-11-07 김태일 지시).
            # 승인 대기로 두면 2026-11-07 07:49-15:44 처럼 2차가 확인 요청에 답하는데도
            # 모든 요청이 안내문으로 끝난다. 다시 로그인하는 일은 소유자 DM 이 알린다.
            state = self._begin_switch(request, EngineSwitcher.AUTH_FAILURE, auth_failure,
                                       auto_approve=True)
            if state.get("approval") == "approved":
                return self._run_secondary(request)
            return self._announce_switch(response)
        return response

    def _announce_switch(self, response: EngineResponse) -> EngineResponse:
        """전환을 시작한 그 요청에도 사람에게 보일 답을 낸다.

        1차의 실패 응답은 기본이 비공개다 - codex 의 종료코드 1 이 그렇다.
        그대로 돌려주면 pipeline 이 아무것도 게시하지 않아, 전환을 만든 그
        요청 하나만 답 없이 끝난다 (sca-3hh4). 두 번째 요청부터는 run() 의
        승인 대기 분기가 같은 문구를 낸다.

        실패 사유도 전환 계기로 바꿔 쓴다. nonzero_exit 으로 남기면 새 세션
        재시도 대상이 되어 같은 자격으로 한 번 더 실패한다.
        """
        if response.user_facing and response.body.strip():
            return response
        return dataclasses.replace(
            response, body=self.switcher.limit_reply(), user_facing=True,
            failure_reason=self.switcher.reason(),
        )

    def _begin_switch(self, request: EngineRequest, reason: str, detail: str,
                      auto_approve: bool = False) -> Mapping[str, Any]:
        probe_ok, probe_detail = self._probe_secondary(request)
        state = self.switcher.begin_switch(
            detail, engine_name=self.secondary.name, reason=reason,
            probe_ok=probe_ok, probe_detail=probe_detail,
            # 확인 요청이 실패한 2차를 승인하면 모든 요청이 2차 실패로 끝난다.
            auto_approve=auto_approve and probe_ok,
        )
        self._notify_switch(state, reason, detail, probe_ok, probe_detail)
        return state

    def _bot_name(self) -> str:
        return self.primary.profile.display_name or self.primary.profile.name

    def _send(self, body: str, what: str) -> None:
        """소유자 DM 을 보내되, 실패가 요청 처리를 끝내지 않게 삼킨다."""
        if self._notify is None:
            return
        try:
            self._notify(body)
        except Exception:
            log.exception("%s 알림에 실패했다", what)

    def _notify_switch(self, state: Mapping[str, Any], reason: str, detail: str,
                       probe_ok: bool, probe_detail: str) -> None:
        """전환이 시작되면 소유자 DM 으로 사유·시각·확인결과를 push 한다 (B24·B25).

        '엔진 상태' 를 조회하지 않아도 전환 사실이 바로 소유자에게 닿게 한다.
        """
        if self._notify is None:
            return
        body = switch_dm_body(
            bot_name=self._bot_name(), from_engine=self.primary.name,
            to_engine=self.secondary.name, reason=reason, detail=detail,
            probe_ok=probe_ok, probe_detail=probe_detail,
            switched_at=float(state.get("switched_at", self._clock())),
            approved=state.get("approval") == "approved",
        )
        self._send(body, "실행기 전환")

    def _notify_recovery(self) -> None:
        """기본 엔진이 복귀하면 되돌렸다는 DM 을 발송한다 (B26)."""
        if self._notify is None:
            return
        body = recovery_dm_body(
            bot_name=self._bot_name(), primary_engine=self.primary.name,
            recovered_at=self._clock(),
        )
        self._send(body, "실행기 복귀")

    def _run_secondary(self, request: EngineRequest) -> EngineResponse:
        """Handles this turn on the approved fallback engine.

        A different engine can't continue a session, so this opens a
        new one. Model naming also differs per engine, so a channel's model
        can't carry over; every requester gets the secondary's default model.
        The owner is not raised to the secondary's owner model any more: the
        primary picks one model for every requester (A13, 김태일 2026-10-22),
        and raising only here split the grades again (supersedes sca-14h).
        """
        self._active = self.secondary
        fallback_request = EngineRequest(
            prompt=request.prompt, system_prompt=request.system_prompt,
            session_id=None, resume=False,
            model=None, effort=request.effort,
            workdir=request.workdir, readable_dirs=request.readable_dirs,
            tools=request.tools, trust_level=request.trust_level,
            # codex and gemini are the engines that read input_mark_for, so
            # this is the one path where the delegated mark is used at all.
            # Dropped, the turn keeps its tools and delegated note but gets
            # the "read it as material" mark.
            delegated=request.delegated,
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
            # Per-turn facts are the same turn's on either engine: dropping them
            # lost TURN_ID, the Stop hook's channel sets and the MCP turn_env
            # tokens for every turn served by the fallback (C10).
            turn_context=request.turn_context,
            engine_env=request.engine_env,
            # The tier the capability record names. Dropped, a fallback turn's
            # record lost it (A12 review).
            turn_tier=request.turn_tier,
            retarget=request.retarget,
        )
        return self.runner.run(self.secondary, self._fit(self.secondary, fallback_request))

    def _probe_primary_recovery(self, request: EngineRequest) -> EngineResponse | None:
        """Probes whether the primary has recovered, using a real request.

        Only happens on the request path — a human is waiting there,
        so they should be the first to benefit once it's recovered,
        and this turn can be handled immediately.
        """
        self._active = self.primary
        response = self.runner.run(self.primary, self._fit(self.primary, request))
        self.switcher.mark_probed(time.time())
        if response.ok and self.primary.detect_usage_limit(response) is None:
            self.switcher.recover()
            self._notify_recovery()
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
            readable_dirs=(), trust_level=request.trust_level,
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
