"""Receives Slack events and enqueues jobs. Does not call the engine — a
separate worker process consumes the queue for that.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any, ClassVar

from ..admin.command import AdminContext
from ..admin.router import AdminRouter
from ..jobs.ports import JobQueue
from ..reliability.dedup import DeduplicationTracker
from ..slack.assistant import AssistantPanel
from ..slack.attachments import AttachmentStore
from ..slack.gateway import SlackGateway
from ..slack.listener import EventListener
from ..slack.reactions import ReactionMarker
from .context import RequestContext
from .spawn import TaskSpawner

log = logging.getLogger(__name__)

# Admin commands match on `text.strip() in ("도움말", ...)`, but mention events
# carry `<@U123> 도움말`, so this must be stripped before command dispatch. Matches
# `<@U123|displayname>` too (no space inside the brackets), not just the bare form.
MENTION_RE = re.compile(r"<@[^>\s]+>")

# (emoji, channel, message_ts, actor)
ReactionCallback = Callable[[str, str, str, str], None]
# (channel, thread_ts, body)
ReplyCallback = Callable[[str, str, str], None]
AdminContextBuilder = Callable[[RequestContext], AdminContext]


class IngressService:
    def __init__(
        self,
        listener: EventListener,
        dedup: DeduplicationTracker,
        queue: JobQueue,
        reactions: ReactionMarker,
        attachments: AttachmentStore,
        admin_router: AdminRouter,
        admin_context_builder: AdminContextBuilder,
        reply: ReplyCallback,
        allowed_reactions: frozenset[str],
        on_reaction: ReactionCallback,
        spawn: TaskSpawner,
        # Retry cap for a failed job; 0 means unlimited. Takes just this value rather
        # than the whole settings object since ingress doesn't need anything else from it.
        job_max_attempts: int = 0,
        # How many times to try storing one request before giving up. Slack acks
        # the socket event before this runs, so it never redelivers a request
        # that failed here — this retry is the only recovery (sca-9r6).
        enqueue_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        # None means this bot has no agent panel wiring; the event is then
        # neither subscribed to nor handled.
        assistant: AssistantPanel | None = None,
    ) -> None:
        self._listener = listener
        self._dedup = dedup
        self._queue = queue
        self._reactions = reactions
        self._attachments = attachments
        self._admin_router = admin_router
        self._admin_context_builder = admin_context_builder
        self._reply = reply
        self._allowed_reactions = allowed_reactions
        self._on_reaction = on_reaction
        self._spawn = spawn
        self._job_max_attempts = job_max_attempts
        self._enqueue_attempts = max(1, enqueue_attempts)
        self._sleep = sleep
        self._assistant = assistant

    def register(self, gateway: SlackGateway) -> None:
        gateway.on("app_mention", self.handle_app_mention)
        gateway.on("message", self.handle_message)
        gateway.on("reaction_added", self.handle_reaction)
        if self._assistant is not None:
            gateway.on("assistant_thread_started", self.handle_assistant_thread_started)

    def handle_app_mention(self, event: Mapping[str, Any]) -> None:
        self._process(self._listener.from_app_mention(event), event)

    def handle_message(self, event: Mapping[str, Any]) -> None:
        self._process(self._listener.from_message(event), event)

    def handle_assistant_thread_started(self, event: Mapping[str, Any]) -> None:
        if self._assistant is None:
            return
        self._assistant.thread_started(event)

    def handle_reaction(self, event: Mapping[str, Any]) -> None:
        # Caught here so one bad event doesn't stop future reaction events from being handled.
        try:
            result = self._listener.from_reaction(event, self._allowed_reactions)
            if result is None:
                return
            # A review calls the engine and takes minutes; running it here
            # would block every other Slack event for that whole time.
            self._spawn.spawn(f"점검:{result[0]}", lambda: self._on_reaction(*result))
        except Exception:
            log.exception("리액션 이벤트 처리 실패: %s", event.get("reaction"))

    def _process(self, ctx: RequestContext | None, event: Mapping[str, Any]) -> None:
        # Exceptions are swallowed (one queue-storage failure shouldn't block all
        # later requests) but always logged, so a dropped request is distinguishable
        # from nothing having happened.
        try:
            if ctx is None:
                return
            if self._dedup.already_seen_event(ctx.channel, ctx.ts):
                return

            ctx = replace(ctx, text=MENTION_RE.sub("", ctx.text).strip())

            admin_ctx = self._admin_context_builder(ctx)
            admin_result = self._admin_router.dispatch(ctx.text, admin_ctx)
            if admin_result is not None:
                self._reply(ctx.channel, ctx.thread_ts, admin_result.message)
                return

            request = self._merge_attachments(ctx, event)

            try:
                queued = self._store(request)
            except Exception:
                # The dedup record was made before this point, so leaving it
                # would block a later redelivery too (sca-if6). Past the admin
                # dispatch, nothing here has an effect outside the queue, so
                # re-running the same event is safe.
                self._dedup.forget_event(request.channel, request.ts)
                self._report_not_accepted(request)
                raise
            if not queued:
                # Already queued — don't mark it twice.
                return
            self._mark_accepted(request)
        except Exception:
            channel = ctx.channel if ctx is not None else event.get("channel")
            ts = ctx.ts if ctx is not None else event.get("ts")
            log.exception("요청 접수 실패: %s:%s", channel, ts)

    def _store(self, ctx: RequestContext) -> bool:
        last: Exception | None = None
        for attempt in range(self._enqueue_attempts):
            try:
                return self._queue.enqueue(ctx, max_attempts=self._job_max_attempts)
            except Exception as exc:
                last = exc
                log.warning("요청 적재 실패, 다시 시도한다 (%d회차) : %s", attempt + 1, exc)
                if attempt + 1 < self._enqueue_attempts:
                    self._sleep(self._enqueue_retry_wait_sec * (attempt + 1))
        raise last if last is not None else RuntimeError("요청을 적재하지 못했다")

    #: Backoff step between store attempts. Short — the socket handler is
    #: blocked while this runs and Slack's other events wait behind it.
    _enqueue_retry_wait_sec: ClassVar[float] = 0.2

    def _report_not_accepted(self, ctx: RequestContext) -> None:
        try:
            self._reactions.mark_failed(ctx.channel, ctx.ts)
            self._reply(
                ctx.channel, ctx.thread_ts,
                "요청을 접수하지 못했습니다. 다시 불러 주십시오.",
            )
        except Exception as exc:  # noqa: BLE001 - a failed notice must not hide the original failure
            log.warning("접수 실패 안내를 보내지 못했다 : %s", exc)

    def _mark_accepted(self, ctx: RequestContext) -> None:
        """One mark, not both: hourglass if it has to wait, eyes if not.

        The queue serializes per thread_ts, so a request waits exactly when
        another unfinished job already holds its thread. Asked after enqueue so
        this request's own row is in the table and can be excluded by ts.
        Marking both made hourglass meaningless — every request carried it.
        """
        if self._queue.blocked_on_thread(ctx.thread_ts, ctx.ts):
            self._reactions.mark_waiting(ctx.channel, ctx.ts)
        else:
            self._reactions.mark_processing(ctx.channel, ctx.ts)

    def _merge_attachments(
        self, ctx: RequestContext, event: Mapping[str, Any]
    ) -> RequestContext:
        # Keeps the original Slack file fields and just adds local_path — files is
        # meant to hold whatever shape Slack sent, not a save-result-only type.
        saved = self._attachments.save(event)
        if not saved:
            return ctx
        originals = {f.get("name"): f for f in (event.get("files") or [])}
        merged_files = []
        for item in saved:
            original = dict(originals.get(item.name) or {})
            original["name"] = item.name
            original["mimetype"] = item.kind
            original["local_path"] = item.path
            merged_files.append(original)
        return replace(ctx, files=tuple(merged_files))
