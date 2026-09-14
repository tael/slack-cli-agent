"""Handles a request end to end: session resolution, prompt assembly, engine
call, guard corrections, and posting to Slack. Implements
``core.ports.RequestHandler`` so the worker only needs to know ``HandleOutcome``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from ..auth.policy import AccessPolicy
from ..auth.principal import Principal, TrustLevel
from ..config.channel import ChannelRegistry
from ..engine.base import Engine, EngineRequest, EngineResponse, Usage
from ..engine.runner import EngineInvoker
from ..guard.base import GuardContext
from ..guard.pipeline import GuardPipeline
from ..guard.watch import WatchPromiseGuard
from ..observability.audit import AuditLog
from ..observability.response_archive import ResponseArchive
from ..observability.slow_report import SlowRequestMeta, SlowRequestReporter, tail_output
from ..prompt.composer import SystemPromptComposer
from ..prompt.sections import SILENT_MARK, CompositionContext
from ..reliability.watchjobs import WatchJobPort
from ..session.manager import SessionDecision, SessionManager
from ..session.ports import SessionKey, SessionScope
from ..slack.late_addendum import LateAddendumChecker, ThreadConsumption, late_addendum_prompt
from ..slack.publisher import MessagePublisher
from ..slack.reactions import ReactionMarker
from ..slack.transcript import TranscriptBuilder
from .context import RequestContext
from .ports import HandleOutcome

log = logging.getLogger(__name__)

class RequestPipeline:
    def __init__(
        self,
        *,
        access_policy: AccessPolicy,
        transcript_builder: TranscriptBuilder,
        prompt_composer: SystemPromptComposer,
        session_manager: SessionManager,
        engine: Engine,
        # Not the raw runner — calling EngineRunner.run() directly would skip
        # FallbackEngine.run(), so a usage-limit hit wouldn't trigger the fallback switch.
        invoker: EngineInvoker,
        guard_pipeline: GuardPipeline,
        publisher: MessagePublisher,
        audit: AuditLog,
        channels: ChannelRegistry,
        default_workdir: Path,
        owner_user_id: str = "",
        reactions: ReactionMarker | None = None,
        name_resolver: Callable[[str], str] = lambda user_id: user_id,
        mention_table: Callable[[], Mapping[str, str]] = dict,
        slow_reporter: SlowRequestReporter | None = None,
        # Optional to avoid an extra Slack lookup where it's not needed.
        participants: Callable[[str, str], tuple[tuple[str, str], ...]] | None = None,
        # Optional to avoid an extra Slack lookup where it's not needed.
        late_addendum: LateAddendumChecker | None = None,
        # Shared with the caller so both the late-addendum check and the queue see
        # the same consumed-messages record.
        consumption: ThreadConsumption | None = None,
        watch_queue: WatchJobPort | None = None,
        # Feeds the learning batch — without it, that batch has nothing to read.
        response_archive: ResponseArchive | None = None,
        now: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._access = access_policy
        self._transcript = transcript_builder
        self._composer = prompt_composer
        self._sessions = session_manager
        self._engine = engine
        self._invoker = invoker
        self._guards = guard_pipeline
        self._publisher = publisher
        self._audit = audit
        self._channels = channels
        self._default_workdir = default_workdir
        self._owner_user_id = owner_user_id
        self._reactions = reactions
        self._name_resolver = name_resolver
        self._mention_table = mention_table
        self._slow_reporter = slow_reporter
        self._participants = participants
        self._late_addendum = late_addendum
        self._consumption = consumption
        self._watch_queue = watch_queue
        self._response_archive = response_archive
        self._now = now
        self._monotonic = monotonic

    def handle(self, ctx: RequestContext) -> HandleOutcome:
        try:
            return self._handle(ctx)
        except Exception as exc:  # noqa: BLE001 — must not raise; the worker loop shouldn't die on one bad request
            failure = str(exc) or exc.__class__.__name__
            self._mark_failed(ctx)
            self._record_best_effort(ctx, failure)
            return HandleOutcome(ok=False, failure=failure)

    def _report_if_slow(
        self,
        ctx: RequestContext,
        decision: Any,
        model: str,
        effort: str,
        elapsed: float,
        mono_elapsed: float,
        response: EngineResponse,
        started: float,
    ) -> None:
        # A reporting failure must not flip an already-successful response to a failure.
        if self._slow_reporter is None:
            return
        config = self._channels.get(ctx.channel)
        try:
            self._slow_reporter.maybe_report(
                SlowRequestMeta(
                    elapsed_wall=elapsed,
                    mono_elapsed=mono_elapsed,
                    # Session records accumulate events across requests; without this,
                    # the breakdown covers the whole session, not just this request.
                    started=started,
                    model=model,
                    model_actual=response.model_actual,
                    effort=effort,
                    num_turns=response.turns,
                    reason=response.failure_reason,
                    session_id=decision.session_id or "",
                    resume=decision.resume,
                    channel=ctx.channel,
                    channel_name=config.name if config else ctx.channel,
                    text=ctx.text,
                    usage=response.usage,
                    # None rather than "" for a missing key, so the report doesn't
                    # render an empty block.
                    stdout_tail=tail_output(response.raw.get("stdout")) or None,
                    stderr_tail=tail_output(response.raw.get("stderr")) or None,
                )
            )
        except Exception:
            log.exception("느린 요청 보고에 실패했다")

    def _handle(self, ctx: RequestContext) -> HandleOutcome:
        start = self._now()
        # Wall clock alone can't distinguish "actually slow" from "device slept".
        mono_start = self._monotonic()
        self._mark_processing(ctx)

        principal = self._access.principal_for(ctx.channel, ctx.user)
        model = self._access.model_for(principal)
        effort = self._access.effort_for(principal, ctx.text)

        config = self._channels.get(ctx.channel)
        workdir = config.workdir if (config and config.workdir) else self._default_workdir
        rich = bool(config and config.rich)
        channel_mode = config.mode if config else "default"
        channel_slug = config.name if config else ctx.channel
        chat_level = config.chat if config else "normal"
        scope = config.session_scope if config else SessionScope.THREAD.value

        key = self._session_key(ctx, scope)
        decision = self._sessions.resolve(key, engine=self._engine.name)

        prompt = self._build_prompt(ctx, scope, decision)
        system_prompt = self._compose_system_prompt(
            ctx, principal, channel_mode, channel_slug, rich, chat_level
        )

        request = EngineRequest(
            prompt=prompt,
            system_prompt=system_prompt,
            session_id=decision.session_id,
            resume=decision.resume,
            model=model,
            effort=effort,
            workdir=workdir,
            trust_level=principal.trust,
        )
        response = self._invoker.invoke(request)

        if not response.ok and self._sessions.should_retry_with_new_session(
            response.failure_reason or ""
        ):
            decision = self._sessions.reset(key, engine=self._engine.name)
            prompt = self._build_prompt(ctx, scope, decision)
            request = replace(request, session_id=decision.session_id, resume=False, prompt=prompt)
            response = self._invoker.invoke(request)

        elapsed = self._now() - start
        # Called once here, before branching into success/failure/silent — calling
        # it in each branch would report the same request multiple times.
        self._report_if_slow(ctx, decision, model, effort, elapsed, self._monotonic() - mono_start, response, start)

        if not response.ok:
            failure = response.failure_reason or "엔진 실행 실패"
            self._record(ctx, decision, model, effort, elapsed, ok=False, usage=None, failure=failure)
            self._mark_failed(ctx)
            return HandleOutcome(ok=False, failure=failure)

        # Only adopt the engine's session ID after success — resuming from a
        # failed run's session ID would break the next request too.
        if response.session_id and self._sessions.adopt_engine_session(
            key, decision.session_id, self._engine.name, response.session_id
        ):
            decision = replace(decision, session_id=response.session_id)

        self._sessions.touch(key, ctx.ts)

        if response.body.strip() == SILENT_MARK:
            self._record(ctx, decision, model, effort, elapsed, ok=True, usage=response.usage)
            self._mark_silent(ctx)
            return HandleOutcome(ok=True, silent=True)

        body, previous_body = self._absorb_late_addendum(ctx, scope, decision, request, response)
        body, watch_desc = self._apply_guards(body, ctx, principal, request, decision, previous_body)
        body = self._publisher.apply_elapsed_model_line(body, model, rich)
        posted_ts = self._publisher.post(ctx.channel, ctx.thread_ts, body, rich) or ""
        self._archive_response(ctx, channel_slug, body, response)

        self._record(ctx, decision, model, effort, elapsed, ok=True, usage=response.usage)
        if watch_desc and self._register_watch(ctx, principal, watch_desc):
            # Not "done" — marking it done would exclude it from catch-up recovery.
            self._mark_watch(ctx)
        else:
            self._mark_done(ctx)
        return HandleOutcome(ok=True, posted_ts=posted_ts)

    def _archive_response(
        self, ctx: RequestContext, channel_slug: str, body: str, response: EngineResponse
    ) -> None:
        # The message is already sent; an archive failure must not flip this to a failure.
        if self._response_archive is None:
            return
        try:
            self._response_archive.record(
                channel_slug=channel_slug,
                user=ctx.user,
                thread_ts=ctx.thread_ts,
                question=ctx.text,
                body=body,
                ok=True,
                elapsed_sec=response.elapsed,
                turns=response.turns,
            )
        except Exception:
            log.exception("응답 기록에 실패했다")

    def _session_key(self, ctx: RequestContext, scope: str) -> SessionKey:
        key_value = ctx.channel if scope == SessionScope.CHANNEL else f"{ctx.channel}:{ctx.thread_ts}"
        return SessionKey(scope=scope, key=key_value)

    def _build_prompt(self, ctx: RequestContext, scope: str, decision: SessionDecision) -> str:
        if decision.resume:
            transcript = self._transcript.thread_transcript(
                ctx.channel, ctx.thread_ts, ctx.ts, scope=scope, after_ts=decision.after_ts,
            )
        else:
            transcript = self._transcript.thread_transcript(
                ctx.channel, ctx.thread_ts, ctx.ts, scope=scope,
            )
        return self._transcript.with_history(transcript, ctx.text)

    def _compose_system_prompt(
        self,
        ctx: RequestContext,
        principal: Principal,
        channel_mode: str,
        channel_slug: str,
        rich: bool,
        chat_level: str,
    ) -> str:
        is_owner = principal.trust is TrustLevel.OWNER
        asker_name = "" if is_owner else (self._name_resolver(ctx.user) or ctx.user)
        composition_ctx = CompositionContext(
            principal=principal,
            prompt=ctx.text,
            channel_mode=channel_mode,
            channel_slug=channel_slug,
            is_rich=rich,
            asker_name=asker_name,
            asker_id="" if is_owner else ctx.user,
            unaddressed=ctx.unaddressed,
            chat_level=chat_level,
            people=self._present_people(ctx),
        )
        return self._composer.compose(composition_ctx)

    def _present_people(self, ctx: RequestContext) -> tuple[tuple[str, str], ...]:
        # A lookup failure just omits this from the prompt; it must not block the reply.
        if self._participants is None:
            return ()
        try:
            return self._participants(ctx.channel, ctx.thread_ts)
        except Exception as exc:  # noqa: BLE001 — a lookup failure must not block the reply, just omit this from the prompt
            log.warning("함께 있는 사람을 세지 못했다 : %s", exc)
            return ()

    def _absorb_late_addendum(
        self,
        ctx: RequestContext,
        scope: str,
        decision: SessionDecision,
        request: EngineRequest,
        response: EngineResponse,
    ) -> tuple[str, str | None]:
        # Returns (body to post, previous body). previous_body is None unless a
        # re-run happened — the loss-detection guard only runs when it's set. If the
        # re-run itself fails, the original body is posted and nothing is marked
        # consumed, since that message still hasn't been answered.
        body = response.body
        if self._late_addendum is None:
            return body, None
        try:
            addendum, addendum_ts = self._late_addendum.check(
                ctx.channel, ctx.thread_ts, ctx.ts, scope
            )
        except Exception as exc:  # noqa: BLE001 — a failed pre-send recheck must not discard the answer already produced
            log.warning("발송 전 스레드 재확인 실패 : %s", exc)
            return body, None
        if not addendum:
            return body, None

        rerun = self._invoker.invoke(replace(
            request,
            prompt=late_addendum_prompt(addendum),
            resume=True,
            session_id=decision.session_id,
        ))
        new_body = (rerun.body or "").strip()
        if not rerun.ok or not new_body or new_body == SILENT_MARK:
            return body, None

        if self._consumption is not None:
            self._consumption.mark(ctx.thread_ts, addendum_ts)
        return rerun.body, body

    def _apply_guards(
        self,
        body: str,
        ctx: RequestContext,
        principal: Principal,
        request: EngineRequest,
        decision: SessionDecision,
        previous_body: str | None = None,
    ) -> tuple[str, str]:
        # A rewrite rerun is chained at most once — the second guard run
        # (is_rewrite_retry=True) has its own `rerun` ignored, which is what caps
        # this at one retry instead of looping.
        is_owner = principal.trust is TrustLevel.OWNER
        guard_ctx = GuardContext(
            channel=ctx.channel,
            thread_ts=ctx.thread_ts,
            asker_id=ctx.user,
            is_owner=is_owner,
            owner_user_id=self._owner_user_id,
            mention_names=self._mention_table(),
            previous_body=previous_body,
        )
        result = self._guards.run(body, guard_ctx)
        if result.rerun is None:
            return result.body, _watch_desc_of(result)

        rerun_request = replace(
            request, prompt=result.rerun.rewrite_prompt, resume=True, session_id=decision.session_id,
        )
        rerun_response = self._invoker.invoke(rerun_request)
        if not rerun_response.ok:
            return result.body, _watch_desc_of(result)

        retry_ctx = replace(guard_ctx, previous_body=result.body, is_rewrite_retry=True)
        final_result = self._guards.run(rerun_response.body, retry_ctx)
        return final_result.body, _watch_desc_of(final_result)

    def _register_watch(self, ctx: RequestContext, principal: Principal, description: str) -> bool:
        if self._watch_queue is None:
            return False
        try:
            self._watch_queue.enqueue(
                ctx.channel, ctx.thread_ts, description,
                msg_ts=ctx.ts, trust=principal.trust,
            )
        except Exception as exc:  # noqa: BLE001 — the answer is already sent; a retry on this failure would double-post it
            log.warning("감시 등록 실패 : %s", exc)
            return False
        log.info("감시 등록 : %s", description[:80])
        return True

    def _record(
        self,
        ctx: RequestContext,
        decision: SessionDecision,
        model: str,
        effort: str,
        elapsed: float,
        *,
        ok: bool,
        usage: Usage | None,
        failure: str = "",
    ) -> None:
        extra: dict[str, Any] = {}
        if failure:
            extra["failure"] = failure
        first_reaction_sec = self._first_reaction_sec(ctx)
        queue_wait_sec = self._queue_wait_sec(ctx)
        self._audit.record_request(
            channel=ctx.channel,
            thread_ts=ctx.thread_ts,
            message_ts=ctx.ts,
            session_id=decision.session_id,
            resumed=decision.resume,
            model=model,
            effort=effort,
            elapsed=elapsed,
            ok=ok,
            first_reaction_sec=first_reaction_sec,
            queue_wait_sec=queue_wait_sec,
            usage=asdict(usage) if usage is not None else None,
            **extra,
        )

    def _record_best_effort(self, ctx: RequestContext, failure: str) -> None:
        try:
            self._audit.record_request(
                channel=ctx.channel,
                thread_ts=ctx.thread_ts,
                message_ts=ctx.ts,
                session_id="",
                resumed=False,
                model="",
                effort="",
                elapsed=0.0,
                ok=False,
                failure=failure,
            )
        except Exception as exc:  # noqa: BLE001 — an audit-log failure must not mask the original processing failure
            log.warning("최소 감사 기록 실패 : %s", exc)

    @staticmethod
    def _first_reaction_sec(ctx: RequestContext) -> float | None:
        if ctx.first_reaction_at is None:
            return None
        try:
            return ctx.first_reaction_at - float(ctx.ts)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _queue_wait_sec(ctx: RequestContext) -> float | None:
        if not ctx.requeued or ctx.queued_at is None:
            return None
        try:
            return time.time() - ctx.queued_at
        except TypeError:
            return None

    def _mark_processing(self, ctx: RequestContext) -> None:
        if self._reactions is not None:
            self._reactions.mark_processing(ctx.channel, ctx.ts)

    def _mark_done(self, ctx: RequestContext) -> None:
        if self._reactions is not None:
            self._reactions.mark_done(ctx.channel, ctx.ts)

    def _mark_failed(self, ctx: RequestContext) -> None:
        if self._reactions is not None:
            self._reactions.mark_failed(ctx.channel, ctx.ts)

    def _mark_silent(self, ctx: RequestContext) -> None:
        if self._reactions is not None:
            self._reactions.mark_silent(ctx.channel, ctx.ts)

    def _mark_watch(self, ctx: RequestContext) -> None:
        if self._reactions is not None:
            self._reactions.mark_watch(ctx.channel, ctx.ts)


def _watch_desc_of(result: Any) -> str:
    detail = result.details.get(WatchPromiseGuard.name) or {}
    return str(detail.get("watch_desc") or "")
