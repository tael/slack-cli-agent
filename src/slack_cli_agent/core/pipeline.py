"""Handles a request end to end: session resolution, prompt assembly, engine
call, guard corrections, and posting to Slack. Implements
``core.ports.RequestHandler`` so the worker only needs to know ``HandleOutcome``.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..auth.execution_policy import ExecutionPolicy
from ..auth.principal import Principal, TrustLevel
from ..auth.tools import ToolPolicy
from ..config.channel import ChannelConfig, channel_is_rich
from ..config.channel import channel_slug as slug_for
from ..engine.base import NO_DETAIL, Engine, EngineRequest, EngineResponse, FailureDetail, Usage
from ..engine.runner import EngineInvoker
from ..engine.tool_selection import ToolSelection
from ..guard.base import GuardContext
from ..guard.mentions import AddresseeGuard
from ..guard.pipeline import GuardPipeline
from ..guard.rewrite import RewriteLossGuard
from ..guard.watch import WatchPromiseGuard
from ..observability.audit import IncidentKind
from ..observability.progress import ProgressCoordinator
from ..observability.response_archive import ResponseArchive
from ..observability.slow_report import SlowRequestMeta, SlowRequestReporter
from ..prompt.composer import PromptComposer
from ..prompt.sections import CompositionContext, is_silent
from ..reliability.watchjobs import WatchJobPort
from ..reliability.watchresult import WatchResultReader
from ..session.manager import SessionDecision, SessionManager
from ..session.ports import SessionKey, SessionScope
from ..slack.late_addendum import LateAddendumChecker, ThreadConsumption, late_addendum_prompt
from ..slack.mentions import MentionRenderer
from .context import RequestContext
from .pipeline_ports import (
    AccessPolicyPort,
    AuditPort,
    ChannelLookupPort,
    PublisherPort,
    ReactionPort,
    TranscriptPort,
)
from .ports import HandleOutcome

log = logging.getLogger(__name__)

LAUNCHED_WATCH_DESC = "코드가 발급한 이름으로 띄운 백그라운드 작업"
"""Watch description for work launched without a tag.

Fixed on purpose. The description is interpolated into the check turn's
prompt as an instruction, so putting the request text there would let a
sentence in it become that turn's instruction. What the work did comes from
the result file, not from here (codex review).
"""

class RequestPipeline:
    def __init__(
        self,
        *,
        access_policy: AccessPolicyPort,
        transcript_builder: TranscriptPort,
        prompt_composer: PromptComposer,
        session_manager: SessionManager,
        engine: Engine,
        # Not the raw runner — calling EngineRunner.run() directly would skip
        # FallbackEngine.run(), so a usage-limit hit wouldn't trigger the fallback switch.
        invoker: EngineInvoker,
        guard_pipeline: GuardPipeline,
        publisher: PublisherPort,
        audit: AuditPort,
        channels: ChannelLookupPort,
        default_workdir: Path,
        owner_user_id: str = "",
        reactions: ReactionPort | None = None,
        name_resolver: Callable[[str], str] = lambda user_id: user_id,
        group_resolver: Callable[[str], str] | None = None,
        mention_table: Callable[[], Mapping[str, str]] = dict,
        slow_reporter: SlowRequestReporter | None = None,
        # Optional to avoid an extra Slack lookup where it's not needed.
        participants: Callable[[str, str], tuple[tuple[str, str], ...]] | None = None,
        linked_threads: Callable[[str, str], str] | None = None,
        # Optional to avoid an extra Slack lookup where it's not needed.
        late_addendum: LateAddendumChecker | None = None,
        # Shared with the caller so both the late-addendum check and the queue see
        # the same consumed-messages record.
        consumption: ThreadConsumption | None = None,
        watch_queue: WatchJobPort | None = None,
        # Without this the engine gets an empty tool list, which for the Claude
        # CLI means no tools at all.
        tool_policy: ToolPolicy | None = None,
        # Sets the guarantee this request demands of the engine. Defaulted
        # rather than injected so no assembly path can leave it unset and
        # silently drop enforcement (sca-98k).
        execution_policy: ExecutionPolicy | None = None,
        readable_dirs: tuple[Path, ...] = (),
        # Feeds the learning batch — without it, that batch has nothing to read.
        response_archive: ResponseArchive | None = None,
        # Shows which step is running while the engine works. None means no
        # display at all; with one, each channel's `progress` setting decides.
        progress: ProgressCoordinator | None = None,
        now: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        # Injected so a test can state the name instead of matching a uuid.
        new_run_id: Callable[[], str] = lambda: uuid.uuid4().hex[:12],
        # Its own generator: run_id names a result file and is re-minted for the
        # retry, so sharing one source would tie the two lifetimes together.
        new_request_id: Callable[[], str] = lambda: uuid.uuid4().hex[:12],
        watch_results: WatchResultReader,
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
        self._new_run_id = new_run_id
        self._new_request_id = new_request_id
        self._watch_results = watch_results
        self._owner_user_id = owner_user_id
        self._reactions = reactions
        self._name_resolver = name_resolver
        # The prompt body only. Slack's addressee check and this bot's own
        # mention removal read the raw text, so rendering earlier would break
        # them (sca-za2a).
        self._mentions = MentionRenderer(name_resolver, group_resolver)
        self._mention_table = mention_table
        self._slow_reporter = slow_reporter
        self._participants = participants
        self._linked_threads = linked_threads
        self._late_addendum = late_addendum
        self._consumption = consumption
        self._watch_queue = watch_queue
        self._tool_policy = tool_policy
        self._execution_policy = execution_policy or ExecutionPolicy()
        self._readable_dirs = readable_dirs
        self._response_archive = response_archive
        self._progress = progress
        self._now = now
        self._monotonic = monotonic

    def handle(self, ctx: RequestContext) -> HandleOutcome:
        try:
            return self._handle(ctx)
        except Exception as exc:
            failure = str(exc) or exc.__class__.__name__
            # With the reason string alone there is no way to tell where it
            # came from; a frozenset serialization error cost a whole
            # investigation for exactly that (sca-btw).
            log.exception("요청 처리 실패 : %s %s", ctx.channel, ctx.ts)
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
                    # The engine's own session ID when it returned one: Codex
                    # rollout files are named by the thread ID the CLI picked,
                    # so the provisional ID would find no transcript at all on
                    # the first request of a thread.
                    session_id=response.session_id or decision.session_id or "",
                    resume=decision.resume,
                    engine=response.engine,
                    channel=ctx.channel,
                    channel_name=config.name if config else ctx.channel,
                    text=ctx.text,
                    usage=response.usage,
                )
            )
        except Exception:
            log.exception("느린 요청 보고에 실패했다")

    @property
    def audit(self) -> AuditPort:
        return self._audit

    @property
    def tool_policy(self) -> ToolPolicy | None:
        return self._tool_policy

    @property
    def readable_dirs(self) -> tuple[Path, ...]:
        return self._readable_dirs

    def _tools(
        self, principal: Principal, prompt: str, config: ChannelConfig | None
    ) -> ToolSelection:
        if self._tool_policy is None:
            return ToolSelection.unrestricted()
        return ToolSelection.from_names(self._tool_policy.tool_list_for(
            principal, prompt=prompt, skills_enabled=bool(config and config.skills),
        ))

    def _progress_session(self, ctx: RequestContext, log_path: Path | None) -> AbstractContextManager[None]:
        """The progress display for this request, or a pass-through when it has none."""
        if self._progress is None or log_path is None:
            return nullcontext()
        return self._progress.session(ctx.channel, ctx.thread_ts, ctx.user, log_path)

    def _handle(self, ctx: RequestContext) -> HandleOutcome:
        start = self._now()
        # Wall clock alone can't distinguish "actually slow" from "device slept".
        mono_start = self._monotonic()
        self._mark_processing(ctx)

        principal = self._access.principal_for(ctx.channel, ctx.user)
        model = self._access.model_for(principal)
        effort = self._access.effort_for(principal, ctx.text)

        config = self._channels.get(ctx.channel)
        # Resolved so the recorded path means the same thing in the check
        # process, which has its own current directory (sca-6zt).
        workdir = (config.workdir if (config and config.workdir) else self._default_workdir).resolve()
        rich = channel_is_rich(config)
        channel_mode = config.mode if config else "default"
        channel_slug = slug_for(ctx.channel, config)
        chat_level = config.chat if config else "normal"
        scope = config.session_scope if config else SessionScope.THREAD.value

        key = self._session_key(ctx, scope)
        decision = self._sessions.resolve(key, engine=self._engine.name)

        # Minted before the turn so the prompt can name the exact result file.
        # Leaving the name to the engine lets two watches share one file, and
        # then the check turn has no file to look at (sca-17p).
        run_id = self._new_run_id()
        # Separate from run_id, which names a result file and is re-minted for
        # the retry. This one has to survive the retry and the fallback so the
        # audit can group one request's attempts (sca-4ol).
        request_id = self._new_request_id()

        prompt = self._build_prompt(ctx, scope, decision)
        system_prompt, budget_report = self._compose_system_prompt(
            ctx, principal, channel_mode, channel_slug, rich, chat_level, run_id
        )

        progress_log = (
            self._progress.log_path_for(config, ctx.channel, ctx.ts)
            if self._progress is not None else None
        )
        tools = self._tools(principal, ctx.text, config)
        request = EngineRequest(
            prompt=prompt,
            system_prompt=system_prompt,
            session_id=decision.session_id,
            resume=decision.resume,
            model=model,
            effort=effort,
            workdir=workdir,
            tools=tools,
            requirements=self._execution_policy.requirements_for(
                config=config, tools=tools,
            ),
            readable_dirs=self._readable_dirs,
            trust_level=principal.trust,
            progress_log=progress_log,
            request_id=request_id,
            budget_report=budget_report,
        )
        # Covers the retry too: a new-session retry is the same wait for the
        # person watching, and closing the display between the two attempts
        # would read as the request having finished.
        with self._progress_session(ctx, progress_log):
            response = self._invoker.invoke(request)

            if not response.ok and self._sessions.should_retry_with_new_session(
                response.failure_reason or ""
            ):
                decision = self._sessions.reset(key, engine=self._engine.name)
                prompt = self._build_prompt(ctx, scope, decision)
                # A fresh name: the first attempt may already have launched the
                # background command, and reusing the name puts two processes
                # on one result file (codex review).
                run_id = self._new_run_id()
                retry_prompt, retry_budget = self._compose_system_prompt(
                    ctx, principal, channel_mode, channel_slug, rich, chat_level, run_id
                )
                request = replace(
                    request, session_id=decision.session_id, resume=False, prompt=prompt,
                    system_prompt=retry_prompt, budget_report=retry_budget,
                )
                response = self._invoker.invoke(request)

        elapsed = self._now() - start
        # Called once here, before branching into success/failure/silent — calling
        # it in each branch would report the same request multiple times.
        self._report_if_slow(ctx, decision, model, effort, elapsed, self._monotonic() - mono_start, response, start)

        if not response.ok:
            failure = response.failure_reason or "엔진 실행 실패"
            if response.user_facing and response.body.strip():
                self._publisher.post(ctx.channel, ctx.thread_ts, response.body, rich)
            self._record(
                ctx, decision, model, effort, elapsed,
                ok=False, usage=None, turns=response.turns, failure=failure,
                model_actual=response.model_actual,
                failure_detail=response.failure_detail,
            )
            return HandleOutcome(ok=False, failure=failure)

        # Only adopt the engine's session ID after success — resuming from a
        # failed run's session ID would break the next request too.
        if response.session_id and self._sessions.adopt_engine_session(
            key, decision.session_id, self._engine.name, response.session_id
        ):
            decision = replace(decision, session_id=response.session_id)

        self._sessions.touch(key, ctx.ts)

        if is_silent(response.body):
            self._record(ctx, decision, model, effort, elapsed, ok=True, usage=response.usage,
                     turns=response.turns, model_actual=response.model_actual)
            self._audit.record(IncidentKind.SILENT.value, channel=ctx.channel, thread_ts=ctx.thread_ts)
            return HandleOutcome(ok=True, silent=True)

        body, previous_body = self._absorb_late_addendum(ctx, scope, decision, request, response)
        body, watch_desc = self._apply_guards(body, ctx, principal, request, decision, previous_body)
        body = self._publisher.apply_elapsed_model_line(body, model, rich)
        posted_ts = self._publisher.post(ctx.channel, ctx.thread_ts, body, rich) or ""
        self._archive_response(ctx, channel_slug, body, response, elapsed)

        self._record(ctx, decision, model, effort, elapsed, ok=True, usage=response.usage,
                     turns=response.turns, model_actual=response.model_actual)
        watch_desc = watch_desc or self._watch_desc_for_launched(workdir, run_id)
        watching = bool(watch_desc) and self._register_watch(ctx, principal, watch_desc, workdir, run_id)
        # The final mark belongs to the worker: it owns queue completion and the
        # buried-message groups, and marking here too duplicated the Slack call
        # and let the worker's done mark overwrite the watch mark (sca-5sb,
        # sca-t1g). watching=True keeps the message out of the done state, which
        # would exclude it from catch-up recovery.
        return HandleOutcome(ok=True, posted_ts=posted_ts, watching=watching)

    def _archive_response(
        self, ctx: RequestContext, channel_slug: str, body: str, response: EngineResponse, elapsed: float,
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
                # Pipeline-measured wall-clock time, same value the audit log
                # records — not response.elapsed, which some engines (codex)
                # never fill in (always 0.0), leaving the archive at 0.0초 while
                # the audit log showed the real duration.
                elapsed_sec=elapsed,
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
        tagged = self._mentions.render(ctx.text)
        note = self._linked_thread_note(ctx)
        if note:
            tagged = f"{tagged}\n\n{note}"
        return self._transcript.with_history(transcript, tagged)

    def _linked_thread_note(self, ctx: RequestContext) -> str:
        # A lookup failure just omits this from the prompt; it must not block the reply.
        if self._linked_threads is None:
            return ""
        try:
            return self._linked_threads(ctx.text, ctx.channel)
        except Exception as exc:  # noqa: BLE001 — a lookup failure must not block the reply, just omit this from the prompt
            log.warning("링크된 스레드를 읽지 못했다 : %s", exc)
            return ""

    def _compose_system_prompt(
        self,
        ctx: RequestContext,
        principal: Principal,
        channel_mode: str,
        channel_slug: str,
        rich: bool,
        chat_level: str,
        run_id: str = "",
    ) -> tuple[str, dict[str, Any]]:
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
            watch_run_id=run_id,
            watch_out_dir=str(self._watch_results.result_dir),
        )
        text, report = self._composer.compose_with_report(composition_ctx)
        return text, report.as_audit_dict()

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
        self._audit.record(
            IncidentKind.LATE_ADDENDUM.value,
            channel=ctx.channel, thread_ts=ctx.thread_ts,
            ok=rerun.ok, added_chars=len(addendum),
        )
        new_body = (rerun.body or "").strip()
        if not rerun.ok or not new_body or is_silent(new_body):
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
            self._record_guard_incidents(ctx, result.details)
            return result.body, _watch_desc_of(result)

        rerun_request = replace(
            request, prompt=result.rerun.rewrite_prompt, resume=True, session_id=decision.session_id,
        )
        rerun_response = self._invoker.invoke(rerun_request)
        # A rewrite that comes back silent is a refusal, not a rewrite. Sending
        # it would put the mark itself in the channel, and RewriteLossGuard
        # would then merge it onto the previous body (sca-2ak).
        if not rerun_response.ok or is_silent(rerun_response.body):
            self._record_guard_incidents(ctx, result.details)
            return result.body, _watch_desc_of(result)

        retry_ctx = replace(guard_ctx, previous_body=result.body, is_rewrite_retry=True)
        final_result = self._guards.run(rerun_response.body, retry_ctx)
        self._record_guard_incidents(ctx, final_result.details)
        return final_result.body, _watch_desc_of(final_result)

    def _record_guard_incidents(self, ctx: RequestContext, details: Mapping[str, Mapping[str, Any]]) -> None:
        wrong = details.get(AddresseeGuard.name)
        if wrong:
            self._audit.record(
                IncidentKind.WRONG_ADDRESSEE.value, channel=ctx.channel, thread_ts=ctx.thread_ts,
                wrong_target=wrong.get("wrong_target"),
            )
        loss = details.get(RewriteLossGuard.name)
        if loss:
            self._audit.record(
                IncidentKind.REWRITE_LOSS.value, channel=ctx.channel, thread_ts=ctx.thread_ts,
                before_chars=loss.get("before_chars"), after_chars=loss.get("after_chars"),
            )

    def _watch_desc_for_launched(self, workdir: Path, run_id: str) -> str:
        """What to watch when the engine launched background work but emitted
        no tag. Registration used to depend on that tag, so work that ran
        without one finished with nobody reading its exit status (sca-pq5).
        """
        if not self._watch_results.launched(run_id):
            return ""
        return LAUNCHED_WATCH_DESC

    def _register_watch(
        self, ctx: RequestContext, principal: Principal, description: str,
        workdir: Path, run_id: str,
    ) -> bool:
        if self._watch_queue is None:
            return False
        try:
            self._watch_queue.enqueue(
                ctx.channel, ctx.thread_ts, description,
                msg_ts=ctx.ts, trust=principal.trust,
                # Pinned here rather than recomputed at check time: the channel
                # config can change in between, and the result of the work sits
                # under the directory the request actually ran in (sca-6zt).
                workdir=str(workdir),
                run_id=run_id,
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
        turns: int | None = None,
        # The model that actually spent tokens, when the engine reports one.
        # Absent rather than empty when unknown: an empty value would read as
        # "same as asked" in the troubleshooting table (sca-asrp).
        model_actual: str | None = None,
        failure: str = "",
        failure_detail: FailureDetail = NO_DETAIL,
    ) -> None:
        extra: dict[str, Any] = {}
        if model_actual:
            extra["model_actual"] = model_actual
        if failure:
            extra["failure"] = failure
        if failure_detail:
            extra["failure_detail"] = str(failure_detail)
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
            usage=usage.as_audit_dict() if usage is not None else None,
            user=ctx.user,
            turns=turns,
            **extra,
        )

    def _record_best_effort(self, ctx: RequestContext, failure: str) -> None:
        # No EngineResponse survived the exception that got us here, so
        # turns stays unset (unknown) — never 0, which would claim a real value.
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
                user=ctx.user,
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






def _watch_desc_of(result: Any) -> str:
    detail = result.details.get(WatchPromiseGuard.name) or {}
    return str(detail.get("watch_desc") or "")
