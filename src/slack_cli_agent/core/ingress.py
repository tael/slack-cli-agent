"""Receives Slack events and enqueues jobs. Does not call the engine — a
separate worker process consumes the queue for that.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import replace
from typing import Any, ClassVar

from ..admin.admission import AdminAdmission, ClaimUnavailable
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
#: Wraps the DB calls on the event's critical path so they can't hold a
#: socket handler thread for the full lock timeout (sca-9l1).
LockBudget = Callable[[], AbstractContextManager[None]]

# (emoji, channel, message_ts, actor)
ReactionCallback = Callable[[str, str, str, str], None]
# (channel, thread_ts, body)
ReplyCallback = Callable[[str, str, str], None]


class RejectedRequests:
    """Requests this process failed to store, so a later delivery of the same
    one knows to clear the failure mark it left."""

    def __init__(self, max_entries: int = 200) -> None:
        self._keys: set[tuple[str, str]] = set()
        self._max = max_entries
        self._lock = threading.Lock()

    def add(self, channel: str, ts: str) -> None:
        with self._lock:
            if len(self._keys) >= self._max:
                self._keys.clear()
            self._keys.add((channel, ts))

    def discard_key(self, channel: str, ts: str) -> bool:
        with self._lock:
            key = (channel, ts)
            if key not in self._keys:
                return False
            self._keys.discard(key)
            return True


class IngressService:
    def __init__(
        self,
        listener: EventListener,
        # No default: a default here would be a second place deciding what the
        # incoming body looks like, and an assembly that forgets it would go
        # back to dropping every mention (sca-za2a).
        strip_self_mention: Callable[[str], str],
        dedup: DeduplicationTracker,
        queue: JobQueue,
        reactions: ReactionMarker,
        attachments: AttachmentStore,
        # The same object the worker gets: catch-up used to skip this check
        # entirely, so a command received while the bot was down went to the
        # model (sca-oyku).
        admin: AdminAdmission,
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
        # None keeps the database's own long lock wait, which is right for
        # every caller that is not on a socket handler thread.
        lock_budget: LockBudget | None = None,
        # None means this bot has no agent panel wiring; the event is then
        # neither subscribed to nor handled.
        assistant: AssistantPanel | None = None,
    ) -> None:
        self._listener = listener
        self._strip_self_mention = strip_self_mention
        self._dedup = dedup
        self._queue = queue
        self._reactions = reactions
        self._attachments = attachments
        self._admin = admin
        self._reply = reply
        self._allowed_reactions = allowed_reactions
        self._on_reaction = on_reaction
        self._spawn = spawn
        self._job_max_attempts = job_max_attempts
        self._enqueue_attempts = max(1, enqueue_attempts)
        self._not_accepted = RejectedRequests()
        self._sleep = sleep
        self._lock_budget = lock_budget
        self._clock = time.monotonic
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

            ctx = replace(ctx, text=self._strip_self_mention(ctx.text))

            # Inside the budget: the check writes a claim row, and a socket
            # handler thread waiting on that lock holds up every later event
            # (sca-9l1, sca-8m5p).
            try:
                with self._budget():
                    if self._admin.handled(ctx):
                        return
            except ClaimUnavailable:
                # Nobody ran the command. The dedup record is dropped so a
                # redelivery gets through, and the user is told, because
                # catch-up only reaches back one window (sca-8m5p).
                self._dedup.forget_event(ctx.channel, ctx.ts)
                self._report_not_accepted(ctx)
                return

            request = ctx
            try:
                request = self._merge_attachments(ctx, event)
                with self._budget():
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
            self._clear_not_accepted(request)
            try:
                with self._budget():
                    self._mark_accepted(request)
            except Exception as exc:  # noqa: BLE001 - the request is already queued
                # Reporting this as a failed intake would be wrong: the job is
                # in the queue and the worker will run it. Only the reaction is
                # missing (sca-9l1).
                log.warning("접수 표식을 달지 못했다 : %s:%s : %s", request.channel, request.ts, exc)
        except Exception:
            channel = ctx.channel if ctx is not None else event.get("channel")
            ts = ctx.ts if ctx is not None else event.get("ts")
            log.exception("요청 접수 실패: %s:%s", channel, ts)

    @contextmanager
    def _budget(self) -> Iterator[None]:
        with self._lock_budget() if self._lock_budget is not None else nullcontext():
            yield

    def _store(self, ctx: RequestContext) -> bool:
        last: Exception | None = None
        for attempt in range(self._enqueue_attempts):
            started = self._clock()
            try:
                return self._queue.enqueue(ctx, max_attempts=self._job_max_attempts)
            except Exception as exc:  # noqa: BLE001 - any storage error is worth one more try
                last = exc
                log.warning("요청 적재 실패, 다시 시도한다 (%d회차) : %s", attempt + 1, exc)
                if attempt + 1 < self._enqueue_attempts:
                    self._sleep(self._enqueue_retry_wait_sec * (attempt + 1))
            finally:
                self._warn_if_slow(self._clock() - started, attempt + 1)
        raise last if last is not None else RuntimeError("요청을 적재하지 못했다")

    def _warn_if_slow(self, elapsed: float, attempt: int) -> None:
        """A store that fails is logged above; one that merely waits a long
        time is not, and that wait is what holds the handler thread. Without
        this the only visible signal is the lock timeout expiring (sca-9l1)."""
        if elapsed >= self._slow_enqueue_sec:
            log.warning("요청 적재가 오래 걸렸다 (%d회차, %.2f초)", attempt, elapsed)

    #: Backoff step between store attempts. Short — the socket handler is
    #: blocked while this runs and Slack's other events wait behind it.
    _enqueue_retry_wait_sec: ClassVar[float] = 0.1

    #: Above this one store attempt is worth a line even when it succeeds.
    _slow_enqueue_sec: ClassVar[float] = 0.5

    def _report_not_accepted(self, ctx: RequestContext) -> None:
        try:
            self._not_accepted.add(ctx.channel, ctx.ts)
            self._reactions.mark_failed(ctx.channel, ctx.ts)
            self._reply(
                ctx.channel, ctx.thread_ts,
                "요청을 접수하지 못했습니다. 다시 불러 주십시오.",
            )
        except Exception as exc:  # noqa: BLE001 - a failed notice must not hide the original failure
            log.warning("접수 실패 안내를 보내지 못했다 : %s", exc)

    def _clear_not_accepted(self, ctx: RequestContext) -> None:
        """Drops the failure mark a previous rejected delivery left behind.

        Without this the message carries both x and the processing mark while
        the retry runs, which reads as failed and running at once (sca-aqw).
        Only for requests this process rejected — clearing unconditionally
        would add a Slack call to every single request.
        """
        if self._not_accepted.discard_key(ctx.channel, ctx.ts):
            self._reactions.remove(ctx.channel, ctx.ts, "x")

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
