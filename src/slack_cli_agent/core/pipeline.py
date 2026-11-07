"""Handles a request end to end: session resolution, prompt assembly, engine
call, guard corrections, and posting to Slack. Implements
``core.ports.RequestHandler`` so the worker only needs to know ``HandleOutcome``.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..auth.execution_policy import ExecutionPolicy
from ..auth.principal import Principal, TrustLevel
from ..auth.tools import ToolPolicy
from ..config.channel import (
    ChannelConfig,
    advisor_enabled,
    airflow_admins,
    channel_is_rich,
    is_channel_operator,
)
from ..config.channel import channel_slug as slug_for
from ..engine.base import (
    CONTEXT_RESET_RAW_KEY,
    NO_DETAIL,
    TRUNCATED_RAW_KEY,
    Engine,
    EngineRequest,
    EngineResponse,
    FailureDetail,
    Usage,
)
from ..engine.runner import EngineInvoker
from ..reliability.restart import TURN_ID_ENV
from ..engine.tool_selection import ToolSelection
from ..guard.base import GuardContext
from ..guard.figurative import FigurativeGuard
from ..guard.mentions import AddresseeGuard, RecipientResolver
from ..guard.pipeline import GuardPipeline
from ..guard.rewrite import RewriteLossGuard
from ..guard.watch import WatchPromiseGuard, cut_untagged_promise, watch_due_at
from ..observability.audit import IncidentKind
from ..observability.progress import ProgressCoordinator
from ..observability.response_archive import ResponseArchive
from ..observability.slow_report import SlowRequestMeta, SlowRequestReporter
from ..prompt.composer import PromptComposer
from ..prompt.sections import CompositionContext, is_silent
from ..reliability.watchjobs import WatchJobPort
from ..reliability.watchresult import WatchResultReader
from ..session.awaiting import AwaitingPort
from ..session.manager import SessionDecision, SessionManager
from ..session.ports import SessionKey, SessionScope
from ..slack.gate import ResponseGate
from ..slack.generated_images import (
    NOTICE_IMAGE_UPLOAD_FAILED,
    GeneratedImagePoster,
    ImageUpload,
)
from ..slack.late_addendum import LateAddendumChecker, ThreadConsumption, late_addendum_prompt
from ..slack.mentions import MentionRenderer
from ..slack.origin_alive import OriginAliveChecker
from ..slack.tool_post import ToolPostMerger
from ..slack.transcribe import TranscribeFn, transcribe_files
from ..slack.transcript import CurrentMessage
from .channel_kind import is_direct_message_channel
from .context import RequestContext
from .markers import (
    missing_recipient_prompt,
    pick_recipient_tag,
    split_leaked_tags,
    strip_control_tags,
    take_ask_tag,
    take_nowatch_tag,
    take_recipient_tag,
)
from .pipeline_ports import (
    AccessPolicyPort,
    AuditPort,
    ChannelLookupPort,
    PublisherPort,
    ReactionPort,
    TranscriptPort,
)
from .ports import HandleOutcome
from .unposted_render import (
    NOTICE_UNPOSTED_RENDER,
    rendered_and_posted,
    rendered_without_posting,
    unposted_render_prompt,
)

log = logging.getLogger(__name__)


def _domain_applies(workdir: Path | None, home: Path) -> bool:
    """Whether to append the profile's domain knowledge this turn.

    The original loaded its company domain file only when the work directory
    was a real repository outside home, not for the bot's own home -- domain
    facts belong to work in a codebase, not to a chat in the bot's dir. No
    configured workdir reads as home, so nothing is appended.
    """
    if workdir is None:
        return False
    try:
        workdir.resolve().relative_to(home.resolve())
    except ValueError:
        return True
    return False


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
        # Lighter work directory for non-owner turns in a light_context channel
        # (A11). None leaves the feature off and every turn uses default_workdir.
        light_workdir: Path | None = None,
        owner_user_id: str = "",
        reactions: ReactionPort | None = None,
        name_resolver: Callable[[str], str] = lambda user_id: user_id,
        # Whether the asker is a bot, for AskerSection's bot-only branch
        # (B1/B2). Default never-a-bot keeps the generic non-owner note when
        # unwired. Looked up only for non-owner askers, since the owner is a
        # person by definition.
        asker_is_bot: Callable[[str], bool] = lambda user_id: False,
        # J11/B1: when True, a reply with no `[[RECIPIENT:...]]` tag gets one
        # tool-less re-ask for the tag alone; still none, and the turn ends
        # silent. The deployment default is on (RuntimeSettings); this
        # component default stays off so a pipeline built without settings
        # publishes rather than goes quiet.
        require_recipient: bool = False,
        group_resolver: Callable[[str], str] | None = None,
        mention_table: Callable[[], Mapping[str, str]] = dict,
        slow_reporter: SlowRequestReporter | None = None,
        # Optional to avoid an extra Slack lookup where it's not needed.
        participants: Callable[[str, str], tuple[tuple[str, str], ...]] | None = None,
        linked_threads: Callable[[str, str, Sequence[Mapping[str, Any]], bool], str] | None = None,
        # Optional to avoid an extra Slack lookup where it's not needed.
        late_addendum: LateAddendumChecker | None = None,
        # Confirms the question message is still in its thread right before the
        # reply is posted (C34). Optional: without it the reply publishes as
        # before, so a profile with no wiring degrades to "publish anyway".
        origin_alive: OriginAliveChecker | None = None,
        # Writes the final answer over a file message the engine already
        # posted through a tool this turn, instead of a second reply
        # (2026-11-06 duplicate replies). None posts as before.
        tool_post_merger: ToolPostMerger | None = None,
        # Uploads pictures a codex turn made and never posted (D2). The tool
        # post merger then writes the answer over that file message. None
        # leaves them on disk as before.
        generated_images: GeneratedImagePoster | None = None,
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
        # None skips persisting the awaiting-reply state entirely -- the
        # [[ASK]] tag still gets stripped either way, since leaving it in
        # the posted body would expose an internal protocol marker.
        awaiting: AwaitingPort | None = None,
        # 음성 첨부 받아쓰기(G6). 접수가 아니라 여기서 부른다: 클립마다 최대
        # 180초라 소켓 핸들러에서 돌면 접수 표식과 뒤 이벤트가 밀린다.
        # None 이면 음성 첨부에 실패 사유를 싣는다.
        transcriber: TranscribeFn | None = None,
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
        self._light_workdir = light_workdir
        self._new_run_id = new_run_id
        self._new_request_id = new_request_id
        self._watch_results = watch_results
        self._owner_user_id = owner_user_id
        self._reactions = reactions
        self._name_resolver = name_resolver
        self._asker_is_bot = asker_is_bot
        self._require_recipient = require_recipient
        # Reuses asker_is_bot as the is-bot oracle: resolving a RECIPIENT tag
        # needs the same "is this user a bot" lookup for the ASKER→BOT fold and
        # for validating a BOT:id. Forces the leading mention for bot recipients
        # and drops a wrong leading bot-mention from owner/asker replies (D3).
        self._recipient_resolver = RecipientResolver(asker_is_bot)
        # The prompt body only. Slack's addressee check and this bot's own
        # mention removal read the raw text, so rendering earlier would break
        # them (sca-za2a).
        self._mentions = MentionRenderer(name_resolver, group_resolver)
        self._mention_table = mention_table
        self._slow_reporter = slow_reporter
        self._participants = participants
        self._linked_threads = linked_threads
        self._late_addendum = late_addendum
        self._origin_alive = origin_alive
        self._tool_post_merger = tool_post_merger
        self._generated_images = generated_images
        self._consumption = consumption
        self._watch_queue = watch_queue
        self._tool_policy = tool_policy
        self._execution_policy = execution_policy or ExecutionPolicy()
        self._readable_dirs = readable_dirs
        self._response_archive = response_archive
        self._progress = progress
        self._now = now
        self._monotonic = monotonic
        self._awaiting = awaiting
        self._transcriber = transcriber
        self._gate = ResponseGate()

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

    @staticmethod
    def _attachment_dirs(ctx: RequestContext) -> tuple[Path, ...]:
        """Scopes read access to this request's own saved files, not the
        whole attach_dir tree -- that would let one request's engine browse
        every other conversation's attachments (코덱스 8차 리뷰, sca-h2dr)."""
        dirs: list[Path] = []
        for f in ctx.files:
            local_path = f.get("local_path")
            if not local_path:
                continue
            parent = Path(local_path).parent
            if parent not in dirs:
                dirs.append(parent)
        return tuple(dirs)

    def _tools(
        self, principal: Principal, prompt: str, config: ChannelConfig | None,
        *, from_bot: bool = False,
    ) -> ToolSelection:
        if self._tool_policy is None:
            return ToolSelection.unrestricted()
        # The bot check only matters off the owner path: operators never
        # names a bot (mametchi authz.is_operator). The event's bot_id decides
        # first; the users_info lookup reads a failed call as "a person", so
        # it only adds a bot the event did not mark (bot.py:7855).
        is_bot = principal.trust is not TrustLevel.OWNER and (
            from_bot or self._asker_is_bot(principal.user_id)
        )
        return ToolSelection.from_names(
            self._tool_policy.tool_list_for(
                principal, prompt=prompt, skills_enabled=bool(config and config.skills),
                asker_is_bot=is_bot,
            ),
            lookups=self._tool_policy.lookups_for(principal, asker_is_bot=is_bot),
        )

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
        # 처리 표식을 단 뒤에 받아쓴다. 받아쓰는 동안에도 화면에 진행이 보인다.
        if ctx.files:
            ctx = replace(ctx, files=transcribe_files(ctx.files, self._transcriber))

        principal = self._access.principal_for(ctx.channel, ctx.user, ctx.ts)
        model = self._access.model_for(principal)
        effort = self._access.effort_for(principal, ctx.text)

        config = self._channels.get(ctx.channel)
        light_turn = self._light_workdir_for(config, principal)
        # Resolved so the recorded path means the same thing in the check
        # process, which has its own current directory (sca-6zt).
        if light_turn is not None:
            workdir = light_turn.resolve()
        else:
            workdir = (config.workdir if (config and config.workdir) else self._default_workdir).resolve()
        rich = channel_is_rich(config)
        # A flat_reply channel posts straight to the channel, not a thread off
        # the triggering message (A17, mametchi bot.py:6458).
        flat = bool(config and config.flat_reply)
        channel_mode = config.mode if config else "default"
        channel_slug = slug_for(ctx.channel, config)
        chat_level = config.chat if config else "normal"
        # A DM has no threads: every message is a top-level message with its
        # own ts, so thread scope would mint a fresh session per message and
        # the conversation would never carry (mametchi: DM is always channel
        # scope, bot.py:3064). The config default (THREAD) only makes sense in
        # a real channel; a DM with no config would otherwise invert to thread.
        if is_direct_message_channel(ctx.channel):
            scope = SessionScope.CHANNEL.value
        else:
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
        tools = self._tools(principal, ctx.text, config, from_bot=ctx.from_bot)
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
            readable_dirs=self._readable_dirs + self._attachment_dirs(ctx),
            trust_level=principal.trust,
            delegated=principal.delegated,
            progress_log=progress_log,
            request_id=request_id,
            budget_report=budget_report,
            # Opaque to the engine layer: forwarded only so an MCP server whose
            # turn_env names a `channel` key gets this turn's channel as env
            # (mametchi's per-turn MAMETCHI_POST_CHANNEL render guard).
            turn_context={"channel": ctx.channel},
            engine_env={
                **self._engine_env(ctx.channel, config),
                # The marker's second line, so the worker can tell the turn
                # that asked for a restart (reliability/restart.py).
                TURN_ID_ENV: f"{ctx.channel}:{ctx.ts}",
            },
        )
        # Counted rather than derived: the reset is the only trace a broken
        # resume leaves, and without it nothing can say how often context is
        # lost (sca-er9n, bot.py:5142).
        context_reset = False
        # The run_id of the attempt before a session reset, kept so its
        # background work stays watched after the retry mints a new name (C14).
        earlier_run_id = ""
        # Covers the retry too: a new-session retry is the same wait for the
        # person watching, and closing the display between the two attempts
        # would read as the request having finished.
        with self._progress_session(ctx, progress_log):
            response = self._invoker.invoke(request)

            if not response.ok and self._sessions.should_retry_with_new_session(
                response.failure_reason or ""
            ):
                decision = self._sessions.reset(key, engine=self._engine.name)
                context_reset = True
                prompt = self._build_prompt(ctx, scope, decision)
                # A fresh name: the first attempt may already have launched the
                # background command, and reusing the name puts two processes
                # on one result file (codex review).
                earlier_run_id = run_id
                run_id = self._new_run_id()
                # The new session never saw the first attempt, and the thread
                # transcript holds nothing of it either since it never posted.
                # Without this note the retry starts the same work again: a dag
                # trigger runs twice (C14).
                earlier_note = self._earlier_launch_note(earlier_run_id)
                if earlier_note:
                    prompt = f"{prompt}\n\n{earlier_note}"
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
                self._publisher.post(ctx.channel, ctx.thread_ts, response.body, rich, flat=flat)
            self._record(
                ctx, decision, model, effort, elapsed,
                # A failed turn spends tokens too (sca-3g3a). gemini and codex
                # both fill usage on their failure responses; leaving it out
                # made the usage rollup read lower than actual.
                ok=False, usage=response.usage, turns=response.turns, failure=failure,
                model_actual=response.model_actual,
                failure_detail=response.failure_detail,
                truncated=_truncated(response),
                context_reset=context_reset,
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
            # A silent answer still owes the picture the turn made; mametchi
            # uploaded it without a comment.
            self._upload_generated_images(ctx, start, [response], flat)
            self._record(ctx, decision, model, effort, elapsed, ok=True, usage=response.usage,
                     turns=response.turns, model_actual=response.model_actual,
                     truncated=_truncated(response), context_reset=context_reset)
            self._audit.record(IncidentKind.SILENT.value, channel=ctx.channel, thread_ts=ctx.thread_ts)
            return HandleOutcome(ok=True, silent=True)

        # Every engine call that may have run tools for this answer: the render
        # check reads them together, since a rerun can post what the first
        # call only rendered, or render what the first call never touched.
        turn_calls: list[EngineResponse] = [response]
        body, previous_body = self._absorb_late_addendum(
            ctx, scope, decision, request, response, turn_calls,
        )
        body, watch_desc, watch_due_raw = self._apply_guards(
            body, ctx, principal, request, decision, previous_body, turn_calls,
        )
        body = self._apply_ask_tag(body, ctx)
        # The model ends a reply that needs no watch with `[[NOWATCH]]`; it is a
        # signal, not content. Strip it here, after guards and the ask tag, so
        # it never reaches the channel in any render path (sca: rich_render fed
        # the bare tag into a table message's fallback text).
        body, had_nowatch = take_nowatch_tag(body)
        # Resolve the trailing `[[RECIPIENT:...]]` tag before the generic
        # marker strip: a bot recipient gets its leading mention forced here,
        # and with enforcement on a missing tag withholds the body entirely so
        # the owner never relays an answer to the wrong party (D3/J11). Runs
        # after ASK/NOWATCH are taken so the tag the resolver sees is trailing.
        body, blocked_recipient = self._resolve_recipient(body, ctx)
        if blocked_recipient:
            body, blocked_recipient = self._retry_recipient_tag(
                body, ctx, request, decision, response, turn_calls,
            )
        if blocked_recipient:
            # Neither the body nor a notice goes out: the body has no settled
            # recipient, and a notice on a turn the model meant to keep quiet
            # is noise of its own (bot.py:8113-8117). The silent outcome still
            # marks the request done, so catch-up doesn't re-run it.
            self._record(
                ctx, decision, model, effort, elapsed, ok=True, usage=response.usage,
                turns=response.turns, model_actual=response.model_actual,
                truncated=_truncated(response), context_reset=context_reset,
            )
            self._audit.record(IncidentKind.SILENT.value, channel=ctx.channel, thread_ts=ctx.thread_ts)
            return HandleOutcome(ok=True, silent=True)
        body = self._strip_publish_markers(body, ctx)
        # After the markers are taken: the notice must not sit between the body
        # and a trailing tag the strip reads from the end.
        body = self._recover_unposted_render(body, ctx, request, decision, turn_calls)
        body, image_ts = self._post_generated_images(body, ctx, start, turn_calls, flat)
        body = self._publisher.apply_elapsed_model_line(body, model, rich)
        # Right before the post, confirm the question is still in its thread.
        # If the asker deleted it mid-turn, the intake coordinates now point at
        # whatever thread took its place, and posting there strands the answer
        # among strangers' messages (C34, bot.py:8170). Only a definite
        # deletion (``alive`` is False) withholds; an unknown (None) publishes,
        # so a lookup hiccup never discards a good answer. The check sits here,
        # last, so no later rewrite step can re-widen the gap it closes.
        if self._origin_alive is not None and self._origin_alive.alive(ctx.channel, ctx.ts) is False:
            self._audit.record(
                IncidentKind.ORIGIN_DELETED.value, channel=ctx.channel,
                thread_ts=ctx.thread_ts, snippet=body[:300],
            )
            self._record(
                ctx, decision, model, effort, elapsed, ok=True, usage=response.usage,
                turns=response.turns, model_actual=response.model_actual,
                truncated=_truncated(response), context_reset=context_reset,
            )
            return HandleOutcome(ok=True)
        posted_ts = ""
        answer_parts: tuple[str, ...] = ()
        # What the posted message holds. The merger writes the body as is; the
        # publisher turns ```csv blocks into a file note first.
        posted_body = body
        # A file the engine posted itself already sits in the thread as a
        # reply. Writing the answer over it keeps one reply holding both the
        # file and the answer. flat replies go to the channel, not the thread
        # the tool posted into, so they post as usual.
        if self._tool_post_merger is not None and not flat:
            if image_ts:
                # The upload just ran; the thread may not list its message yet.
                posted_ts = self._tool_post_merger.merge(
                    ctx.channel, ctx.thread_ts, start, body, rich, target_ts=image_ts,
                ) or ""
            else:
                posted_ts = self._tool_post_merger.merge(
                    ctx.channel, ctx.thread_ts, start, body, rich,
                ) or ""
            # The merged answer is the tool's reply itself, one message.
            answer_parts = (posted_ts,) if posted_ts else ()
        if not posted_ts:
            receipt = self._publisher.post_receipt(ctx.channel, ctx.thread_ts, body, rich, flat=flat)
            posted_ts = receipt.parent_ts if receipt is not None else ""
            answer_parts = receipt.message_ts if receipt is not None else ()
            if receipt is not None and receipt.body:
                posted_body = receipt.body
        self._archive_response(ctx, body, response, elapsed)

        self._record(ctx, decision, model, effort, elapsed, ok=True, usage=response.usage,
                     turns=response.turns, model_actual=response.model_actual,
                     truncated=_truncated(response), context_reset=context_reset,
                     # The format review rewrites a message from this body: on a
                     # rich channel Slack only hands back the notification
                     # preview, and without the stored body it must not
                     # rewrite at all (review/records.py, B6).
                     answer=_AnswerFields(body=posted_body, question=ctx.text, parts=answer_parts))
        watch_run_id = self._watch_run_id(run_id, earlier_run_id)
        watch_desc = watch_desc or self._watch_desc_for_launched(workdir, watch_run_id)
        watching = bool(watch_desc) and self._register_watch(
            ctx, principal, watch_desc, workdir, watch_run_id, watch_due_at(watch_due_raw),
        )
        # Record how this reply landed on the watch question so the NOWATCH
        # 판정률 can be aggregated (J9): a registered watch, an explicit
        # [[NOWATCH]], or neither (the model left it ambiguous).
        self._audit.record(
            IncidentKind.WATCH_VERDICT.value, channel=ctx.channel, thread_ts=ctx.thread_ts,
            verdict="watch" if watching else ("nowatch" if had_nowatch else "missing"),
        )
        # The final mark belongs to the worker: it owns queue completion and the
        # buried-message groups, and marking here too duplicated the Slack call
        # and let the worker's done mark overwrite the watch mark (sca-5sb,
        # sca-t1g). watching=True keeps the message out of the done state, which
        # would exclude it from catch-up recovery.
        return HandleOutcome(ok=True, posted_ts=posted_ts, watching=watching)

    def _archive_response(
        self, ctx: RequestContext, body: str, response: EngineResponse, elapsed: float,
    ) -> None:
        # The message is already sent; an archive failure must not flip this to a failure.
        if self._response_archive is None:
            return
        try:
            self._response_archive.record(
                # Re-read rather than reuse the slug pinned at the start of the
                # request: a rename that lands mid-request already moved the
                # archive directory, and writing the old name recreates the
                # directory that was just emptied (sca-8aow).
                channel_slug=slug_for(ctx.channel, self._channels.get(ctx.channel)),
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
        body = self._mentions.render(ctx.text)
        note = self._linked_thread_note(ctx)
        if note:
            body = f"{body}\n\n{note}"
        # The head goes on inside with_history, from ts and user -- a caller
        # cannot hand over a bare body and leave the speaker unstated (sca 2026-09-21).
        return self._transcript.with_history(
            transcript,
            CurrentMessage(ts=ctx.ts, user=ctx.user, raw_text=ctx.text, body=body),
        )

    def _light_workdir_for(
        self, config: ChannelConfig | None, principal: Principal
    ) -> Path | None:
        """The lighter work directory this turn should use, or None for the full
        one. A light_context channel routes non-owner turns to it; the owner
        always gets the full directory (A11, mametchi workdir_for bot.py:282).
        None when the channel isn't light_context, the asker is the owner, or no
        light_workdir is configured — so the feature stays inert until an org
        sets the path."""
        if config and config.light_context and principal.trust is not TrustLevel.OWNER:
            return self._light_workdir
        return None

    def _linked_thread_note(self, ctx: RequestContext) -> str:
        # A lookup failure just omits this from the prompt; it must not block the reply.
        if self._linked_threads is None:
            return ""
        try:
            # The event's own unfurl attachments ride along so a link into a
            # channel the bot can't read is still recovered from the preview
            # the sharer's message already carries (C39).
            # The last argument turns on the owner's own Slack session for a
            # link the bot cannot read. Only the profile owner's turn gets it:
            # controller_user_ids are GENERAL (A7) and never get the owner's
            # session, and a non-owner turn would carry a private
            # conversation only the owner can see into its answer (D5).
            as_owner = bool(self._owner_user_id) and ctx.user == self._owner_user_id
            return self._linked_threads(
                ctx.text, ctx.channel, ctx.unfurl_attachments, as_owner
            )
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
        # controller_user_ids are bot accounts; that does not wait on bots_info.
        asker_is_bot = False if is_owner else (
            principal.is_controller or ctx.from_bot or self._asker_is_bot(ctx.user)
        )
        config = self._channels.get(ctx.channel)
        composition_ctx = CompositionContext(
            principal=principal,
            prompt=ctx.text,
            channel_mode=channel_mode,
            channel_slug=channel_slug,
            channel=ctx.channel,
            thread_ts=ctx.thread_ts,
            is_rich=rich,
            asker_name=asker_name,
            asker_id="" if is_owner else ctx.user,
            asker_is_bot=asker_is_bot,
            unaddressed=ctx.unaddressed,
            chat_level=chat_level,
            authority_granted=self._access.is_full_authority(principal),
            # A light turn carries no company domain knowledge, just as the
            # original's light workdir omitted the domain file (A11).
            include_domain=(
                self._light_workdir_for(config, principal) is None
                and _domain_applies(config.workdir if config else None, Path.home())
            ),
            people=self._present_people(ctx),
            files=ctx.files,
            missed_files=ctx.missed_files,
            watch_run_id=run_id,
            watch_out_dir=str(self._watch_results.result_dir),
            airflow_admin=bool(ctx.user) and ctx.user in airflow_admins(config),
            # mametchi bot.py:7855 operator=bool(delegated) or is_operator(...):
            # the ledger, not the operators list, is what lets a bot act here.
            operator=principal.delegated or is_channel_operator(
                config, ctx.user, is_bot=asker_is_bot,
            ),
            advisor_enabled=advisor_enabled(config),
        )
        text, report = self._composer.compose_with_report(composition_ctx)
        return text, report.as_audit_dict()

    def _engine_env(self, channel: str, config: ChannelConfig | None) -> dict[str, str]:
        """Per-turn env the engine's built-in Stop hook reads (finish_review):
        POST_CHANNEL is this turn's channel, ADVISOR_CHANNELS and
        CODE_FIRST_CHANNELS the comma-separated channel IDs whose channels.json
        entry turned the advisor structure (G16) or code_first (G13) on.

        Read per turn so an edit to channels.json applies without a restart.
        A lookup that can only answer `get` (a test double, a plugin store)
        still gets the current channel judged correctly -- the hook only asks
        whether POST_CHANNEL is in the set.
        """
        list_all = getattr(self._channels, "all", None)
        configs: Mapping[str, ChannelConfig] = (
            list_all() if callable(list_all) else ({channel: config} if config else {})
        )
        return {
            "POST_CHANNEL": channel,
            "ADVISOR_CHANNELS": ",".join(
                sorted(cid for cid, c in configs.items() if advisor_enabled(c))
            ),
            "CODE_FIRST_CHANNELS": ",".join(
                sorted(cid for cid, c in configs.items() if c.code_first)
            ),
        }

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
        turn_calls: list[EngineResponse] | None = None,
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
        if turn_calls is not None:
            turn_calls.append(rerun)
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

    def _recover_unposted_render(
        self,
        body: str,
        ctx: RequestContext,
        request: EngineRequest,
        decision: SessionDecision,
        turn_calls: list[EngineResponse],
    ) -> str:
        # A diagram rendered to a local PNG never reaches the thread on its
        # own (D1). One rerun in the same session with the same tools asks for
        # the post; the first answer stays the answer, and the tool post merger
        # writes it over the file message. If the rerun still doesn't post, the
        # body says so, so nobody goes looking for a picture that isn't there.
        # Judged over every call of the turn, failed calls taken off: a post
        # the channel guard refused left nothing in the thread.
        if not rendered_without_posting(turn_calls):
            return body
        # A deleted question gets no answer (C34); the rerun would still put
        # the diagram into whatever thread now holds these coordinates.
        if self._origin_alive is not None and self._origin_alive.alive(ctx.channel, ctx.ts) is False:
            return body
        resumable = self._resumable(turn_calls[-1])
        posted = False
        if resumable:
            log.warning("렌더만 하고 올리지 않았다. 올리게 한다 : %s", ctx.ts)
            rerun = self._invoker.invoke(replace(
                request,
                prompt=unposted_render_prompt(ctx.channel, ctx.thread_ts),
                resume=True,
                session_id=decision.session_id,
            ))
            # Later steps read what the turn posted; the rerun is part of it.
            turn_calls.append(rerun)
            posted = rerun.ok and rendered_and_posted(rerun)
        else:
            log.warning("렌더만 하고 올리지 않았다. 대체 엔진 턴이라 다시 돌리지 않는다 : %s", ctx.ts)
        self._audit.record(
            IncidentKind.RENDER_WITHOUT_POST.value,
            channel=ctx.channel, thread_ts=ctx.thread_ts, recovered=posted,
            rerun=resumable,
        )
        if posted:
            return body
        return f"{body.rstrip()}\n\n{NOTICE_UNPOSTED_RENDER}"

    def _post_generated_images(
        self,
        body: str,
        ctx: RequestContext,
        since: float,
        turn_calls: Sequence[EngineResponse],
        flat: bool,
    ) -> tuple[str, str | None]:
        # Codex's image generation leaves the picture on disk; the model
        # doesn't always post it (D2). Uploaded before the answer; the ts of
        # the file message goes to the tool post merger so the answer lands
        # in it.
        upload = self._upload_generated_images(ctx, since, turn_calls, flat)
        if upload is None or upload.ok:
            return body, upload.ts if upload else None
        return f"{body.rstrip()}\n\n{NOTICE_IMAGE_UPLOAD_FAILED}", None

    def _upload_generated_images(
        self,
        ctx: RequestContext,
        since: float,
        turn_calls: Sequence[EngineResponse],
        flat: bool,
    ) -> ImageUpload | None:
        """None when there was nothing to upload or the question is gone."""
        if self._generated_images is None:
            return None
        images = self._generated_images.pending(since, turn_calls)
        if not images:
            return None
        # A deleted question gets no answer (C34), and no picture either.
        if self._origin_alive is not None and self._origin_alive.alive(ctx.channel, ctx.ts) is False:
            return None
        thread_ts = None if flat else ctx.thread_ts
        return self._generated_images.upload(ctx.channel, thread_ts, images)

    def _resumable(self, response: EngineResponse) -> bool:
        # A fallback turn ran on the secondary, which opens a fresh session on
        # every call and drops the per-turn env (FallbackEngine._run_secondary).
        # A rerun there would get "post the same source" with no source in its
        # context and no POST_CHANNEL, so it is not attempted.
        secondary = getattr(self._engine, "secondary", None)
        if secondary is None:
            return True
        return bool(response.engine) and response.engine != getattr(secondary, "name", None)

    def _apply_guards(
        self,
        body: str,
        ctx: RequestContext,
        principal: Principal,
        request: EngineRequest,
        decision: SessionDecision,
        previous_body: str | None = None,
        turn_calls: list[EngineResponse] | None = None,
    ) -> tuple[str, str, str]:
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
            request_text=ctx.text,
            previous_body=previous_body,
        )
        result = self._guards.run(body, guard_ctx)
        if result.rerun is None:
            self._record_guard_incidents(ctx, result.details)
            return result.body, _watch_desc_of(result), _watch_due_of(result)

        rerun_request = replace(
            request, prompt=result.rerun.rewrite_prompt, resume=True, session_id=decision.session_id,
        )
        rerun_response = self._invoker.invoke(rerun_request)
        if turn_calls is not None:
            turn_calls.append(rerun_response)
        # A rewrite that comes back silent is a refusal, not a rewrite. Sending
        # it would put the mark itself in the channel, and RewriteLossGuard
        # would then merge it onto the previous body (sca-2ak).
        if not rerun_response.ok or is_silent(rerun_response.body):
            self._record_guard_incidents(ctx, result.details)
            body_out = result.body
            # An untagged promise whose rewrite never arrived still must not
            # go out as-is: cut it like a failed retry does (bot.py:8249-8255).
            if result.rerun.guard_name == WatchPromiseGuard.name:
                body_out = cut_untagged_promise(body_out)
            return body_out, _watch_desc_of(result), _watch_due_of(result)

        retry_ctx = replace(guard_ctx, previous_body=result.body, is_rewrite_retry=True)
        final_result = self._guards.run(rerun_response.body, retry_ctx)
        self._record_guard_incidents(ctx, final_result.details)
        return final_result.body, _watch_desc_of(final_result), _watch_due_of(final_result)

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
        fig = details.get(FigurativeGuard.name)
        if fig:
            self._audit.record(
                IncidentKind.FIGURATIVE_FIX.value, channel=ctx.channel, thread_ts=ctx.thread_ts,
                mode=fig.get("mode"), fixes=fig.get("fixes"),
            )

    def _apply_ask_tag(self, body: str, ctx: RequestContext) -> str:
        """Strips `[[ASK]]` and records the awaiting-reply state it (or a
        plain-vocabulary question) implies.

        Runs after guards so the tag is read from the final body, not a
        draft a guard might still rewrite. Writes the state unconditionally
        when a store is wired — a turn that isn't waiting on a reply has to
        clear the thread's previous mark, or an old "awaiting" stays stuck
        on from a prior turn that was never resolved (bot.py's
        `set_awaiting` overwrites every turn for the same reason).
        """
        clean, had_tag = take_ask_tag(body)
        if self._awaiting is not None:
            waiting = had_tag or self._gate.asked_back(clean)
            self._awaiting.set_awaiting(ctx.channel, ctx.thread_ts, waiting)
        return clean

    def _resolve_recipient(self, body: str, ctx: RequestContext) -> tuple[str, bool]:
        """Resolves the trailing `[[RECIPIENT:...]]` tag into a concrete
        recipient and forces the leading mention a bot recipient needs to be
        notified (D3, bot.py:5425-5471 take_recipient_tag).

        Returns `(body, blocked)`. When the tag names a bot (or an ASKER that
        turns out to be a bot), the body comes back with a `<@id>` prefix and
        any wrong leading bot-mention stripped. When there is no resolvable
        recipient and enforcement is on, `blocked=True` tells `handle` to
        withhold the body and post the notice instead (J11). With enforcement
        off the tag is still stripped and the body published as-is, so an
        unconfigured bot never falls silent over a missing tag.
        """
        # The owner is never a bot; skipping it keeps owner turns free of a
        # users.info round trip now that an untagged reply also checks the asker.
        asker = "" if ctx.user == self._owner_user_id else ctx.user
        clean, recipient = self._recipient_resolver.resolve(body, asker)
        if recipient is None and self._require_recipient:
            # Hand back the original body so the audit snippet shows what was
            # withheld; it is not published on this path.
            return body, True
        return clean, False

    def _retry_recipient_tag(
        self,
        body: str,
        ctx: RequestContext,
        request: EngineRequest,
        decision: SessionDecision,
        response: EngineResponse,
        turn_calls: list[EngineResponse],
    ) -> tuple[str, bool]:
        """Asks the model once more for just the recipient tag of a reply that
        came back without one (B1, bot.py:8085-8112 missing_recipient retry).

        The body stays with the code; only the tag is taken from the rerun, so
        a rerun that rewrites the answer can't change what is posted. The
        rerun has every tool forbidden: it resumes the same session, and the
        first turn may already have filed a ticket or sent a message that a
        tool-carrying rerun could repeat. Returns `(body, blocked)` the same
        way `_resolve_recipient` does.
        """
        self._audit.record(
            IncidentKind.MISSING_RECIPIENT.value, channel=ctx.channel,
            thread_ts=ctx.thread_ts, snippet=body[:300],
        )
        if not self._resumable(response):
            # A fallback turn's secondary opens a fresh session that never saw
            # the answer, so it has nothing to choose a recipient for.
            self._audit.record(
                IncidentKind.MISSING_RECIPIENT_RETRY.value, channel=ctx.channel,
                thread_ts=ctx.thread_ts, attempted=False, ok=False, recovered=False, tag=None,
            )
            return body, True
        rerun = self._invoker.invoke(replace(
            request,
            prompt=missing_recipient_prompt(),
            resume=True,
            session_id=decision.session_id,
            tools=ToolSelection.forbid_all(),
        ))
        turn_calls.append(rerun)
        tag = pick_recipient_tag(rerun.body) if rerun.ok else None
        blocked = True
        if tag:
            # Drop a tag the first pass could not resolve (a BOT: id that is no
            # bot) before appending the new one; left in place it would be
            # read again instead of the choice just made.
            held, _ = take_recipient_tag(body)
            body, blocked = self._resolve_recipient(f"{held.rstrip()}\n{tag}", ctx)
        self._audit.record(
            IncidentKind.MISSING_RECIPIENT_RETRY.value, channel=ctx.channel,
            thread_ts=ctx.thread_ts, attempted=True, ok=rerun.ok, recovered=not blocked,
            tag=tag, reply=(rerun.body or "")[:300],
        )
        return body, blocked

    def _strip_publish_markers(self, body: str, ctx: RequestContext) -> str:
        """Last body pass before publish: strips every leftover `[[...]]`
        marker line so it never reaches the channel (bot.py:1564-1614
        strip_control_tags). B1's gaps J18, B4.

        Runs after the ASK/NOWATCH tags are taken and the RECIPIENT tag is
        resolved (`_resolve_recipient`), so those known markers are already
        gone. A declared marker left over is a leak (`control_tag_leak`), an
        undeclared one is `unknown_tag`; both are logged, not silently eaten.
        """
        body, dropped = strip_control_tags(body)
        known, unknown = split_leaked_tags(dropped)
        # A declared marker still here means its own handler missed it (B4):
        # e.g. a mid-body RECIPIENT, or a WATCH_DONE outside a watch check.
        if known:
            self._audit.record(
                IncidentKind.CONTROL_TAG_LEAK.value,
                channel=ctx.channel,
                thread_ts=ctx.thread_ts,
                tags=known,
            )
        if unknown:
            self._audit.record(
                IncidentKind.UNKNOWN_TAG.value,
                channel=ctx.channel,
                thread_ts=ctx.thread_ts,
                tags=unknown,
            )
        return body

    def _earlier_launch_note(self, earlier_run_id: str) -> str:
        """Tells a post-reset retry that the attempt before it already started
        background work, or "" when it started none."""
        if not earlier_run_id or not self._watch_results.launched(earlier_run_id):
            return ""
        path = self._watch_results.path_for(earlier_run_id)
        return (
            "앞선 시도가 이 요청의 백그라운드 작업을 이미 시작했다. "
            f"결과 파일 : {path}\n"
            "같은 작업을 다시 시작하지 말고 이 파일로 진행 상황을 확인해 답해라."
        )

    def _watch_run_id(self, run_id: str, earlier_run_id: str) -> str:
        """Which result file the watch should read after a possible reset.

        The retry's own file wins when it launched work. Otherwise the earlier
        attempt's file is the only work running for this request, and the
        retry's name points at a file that will never appear (C14).
        """
        if not earlier_run_id or self._watch_results.launched(run_id):
            if earlier_run_id and self._watch_results.launched(earlier_run_id):
                log.warning(
                    "세션 리셋 전후 두 시도가 모두 백그라운드 작업을 띄웠다 : %s, %s",
                    earlier_run_id, run_id,
                )
            return run_id
        if self._watch_results.launched(earlier_run_id):
            return earlier_run_id
        return run_id

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
        workdir: Path, run_id: str, due_at: float | None = None,
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
                due_at=due_at,
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
        # None only where no EngineResponse reached this point, so the key is
        # left out rather than written as False -- "we never looked" and "it
        # was not cut" have to stay apart. Where a response exists the key is
        # always written, so its presence marks when the count started.
        truncated: bool | None = None,
        # Always written where a response reached this point, so its presence
        # in a row marks when the count started -- a window with no key at all
        # is "before this was instrumented", not "no resets".
        context_reset: bool = False,
        answer: _AnswerFields | None = None,
    ) -> None:
        extra: dict[str, Any] = {CONTEXT_RESET_RAW_KEY: context_reset}
        if answer is not None:
            extra["answer"] = answer.body
            extra["question"] = answer.question
            # The answer's own message ts, not the thread parent post()
            # returns: matching on the parent picked a later reply's record
            # when the parent itself was flagged (B6 review). Every chunk is
            # kept so a review of a later chunk knows it is only a part.
            if answer.parts:
                extra["answer_ts"] = answer.parts[0]
                extra["answer_parts"] = list(answer.parts)
        if truncated is not None:
            extra[TRUNCATED_RAW_KEY] = truncated
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






@dataclass(frozen=True)
class _AnswerFields:
    """What a posted reply leaves in its audit row for the review paths."""

    body: str
    question: str
    parts: tuple[str, ...]


def _truncated(response: EngineResponse) -> bool:
    return response.raw.get(TRUNCATED_RAW_KEY) is True


def _watch_desc_of(result: Any) -> str:
    detail = result.details.get(WatchPromiseGuard.name) or {}
    return str(detail.get("watch_desc") or "")


def _watch_due_of(result: Any) -> str:
    """Raw `YYYY-MM-DD` string from the `[[WATCH@...]]` tag, if any. Empty
    means no deadline — the same as a plain `[[WATCH: ...]]` tag."""
    detail = result.details.get(WatchPromiseGuard.name) or {}
    return str(detail.get("watch_due") or "")
