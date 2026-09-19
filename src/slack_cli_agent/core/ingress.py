"""Receives Slack events and enqueues jobs. Does not call the engine — a
separate worker process consumes the queue for that.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

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

            ctx = self._merge_attachments(ctx, event)

            try:
                queued = self._queue.enqueue(ctx, max_attempts=self._job_max_attempts)
            except Exception:
                # The dedup record was made before this point, and Slack already
                # ACKed the socket event, so leaving it would suppress the one
                # redelivery that could still recover the request (sca-if6).
                self._dedup.forget_event(ctx.channel, ctx.ts)
                raise
            if not queued:
                # Already queued — don't mark it twice.
                return
            self._mark_accepted(ctx)
        except Exception:
            channel = ctx.channel if ctx is not None else event.get("channel")
            ts = ctx.ts if ctx is not None else event.get("ts")
            log.exception("요청 접수 실패: %s:%s", channel, ts)

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
