"""Catch-up: recovers mentions that arrived while the socket was disconnected.

A persistent job queue handles other restart-recovery cases, but messages
that arrive during a socket outage never produce an event to queue in the
first place — the only way to recover them is to re-read Slack history.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..config.settings import RuntimeSettings
from ..core.context import RequestContext
from ..core.result import Outcome
from ..observability.notices import NoticeCatalog
from ..slack.attachments import AttachmentStore
from ..slack.gate import ResponseGate
from ..slack.identity import BotIdentity
from ..slack.mentions import SelfMentionStripper
from ..slack.message_kind import MessageKind
from ..slack.policy import addresses_someone_else
from .ports import HistoryReader

# Marks the bot puts on a message once it's answered or decided not to.
SILENT_MARK_EMOJI = "zipper_mouth_face"
DONE_EMOJI: frozenset[str] = frozenset({"white_check_mark", SILENT_MARK_EMOJI})

# Retry backoff (seconds). History reads returning empty is usually transient,
# so retries start fast and back off.
CATCHUP_RETRY_BACKOFF: tuple[int, ...] = (30, 60, 120, 300, 600)
# Beyond this, a human should be alerted.
CATCHUP_ALERT_AFTER_SEC: float = 1800
CATCHUP_MAX_THREADS_PER_CHANNEL = 60


def reactions_on(msg: Mapping[str, Any]) -> set[str]:
    return {r.get("name") for r in (msg.get("reactions") or []) if r.get("name")}


def already_handled(msg: Mapping[str, Any]) -> bool:
    # eyes/hourglass can be left behind by a process that died mid-handling,
    # so they don't count as done. A human manually adding white_check_mark
    # also counts, since that's the only way to manually wave off a message.
    return bool(reactions_on(msg) & DONE_EMOJI)


def unanswered(
    thread: Sequence[Mapping[str, Any]],
    asked: Sequence[Mapping[str, Any]],
    *,
    is_self: Callable[[Mapping[str, Any]], bool],
    is_notice: Callable[[str | None], bool],
) -> list[Mapping[str, Any]]:
    """Filters to only the messages that never got a reply.

    Pairs requests to replies in arrival order, since the bot handles one
    request per thread at a time — treating "any later bot reply" as
    covering an earlier request would wrongly mark it answered.
    """
    waiting = {m.get("ts") for m in asked}
    pending: list[Any] = []
    answered: set[Any] = set()
    prev_bot = False
    for x in sorted(thread, key=lambda m: float(m.get("ts") or 0)):
        if is_self(x):
            if is_notice(x.get("text")):
                continue
            if not prev_bot and pending:
                answered.add(pending.pop(0))
            prev_bot = True
            continue
        if x.get("bot_id"):
            continue  # another bot's message; not a human ask or our reply
        prev_bot = False
        if x.get("ts") in waiting:
            pending.append(x.get("ts"))
    return [m for m in asked if m.get("ts") not in answered]


@dataclass(frozen=True)
class CatchupReport:
    """Result of one `sweep` run."""

    missed: list[RequestContext]
    """Representative requests to enqueue. Enqueueing itself is the caller's job."""
    skipped: list[RequestContext]
    """Requests in the same thread superseded by a representative."""
    unchecked_channels: list[str]
    """Channels whose history couldn't be confirmed this round."""


@dataclass(frozen=True)
class RetryStatus:
    """Status `retry_pending` returns for one channel."""

    channel: str
    stuck_sec: float
    alert: bool
    missed: tuple[RequestContext, ...] = ()


class CatchupService:
    def __init__(
        self,
        *,
        history: HistoryReader,
        gate: ResponseGate,
        notices: NoticeCatalog,
        settings: RuntimeSettings,
        identity: BotIdentity,
        message_text: Callable[[Mapping[str, Any]], str] = lambda m: m.get("text") or "",
        now: Callable[[], float] = time.time,
        started_at: float | None = None,
        attachments: AttachmentStore | None = None,
    ) -> None:
        self._history = history
        self._gate = gate
        self._notices = notices
        self._settings = settings
        self._identity = identity
        self._attachments = attachments
        # The live path strips this through ingress. Built from the same class
        # so a body recovered here reads like one received on the socket
        # (sca-za2a).
        self._self_mention = SelfMentionStripper(identity)
        self._message_text = message_text
        self._now = now
        self._started_at = started_at if started_at is not None else now()
        # channel -> (first-failure time, attempt count)
        self._pending: dict[str, tuple[float, int]] = {}
        self._kind = MessageKind()

    def find_missed(self, channel: str, window: float) -> Outcome[list[RequestContext]]:
        """Finds mentions that never got a reply, including thread replies."""
        if not self._identity.known:
            # Without identity we can't judge what counts as a mention. Returning
            # an empty list here would look the same as "nothing missed" and this
            # round would drop out of retry, so the gap never gets recovered.
            return Outcome.unknown("봇 신원을 몰라 부름 판정 불가")

        now = self._now()
        oldest = now - window
        # A thread's parent can be old while a reply just landed, so look back
        # further than the catch-up window itself when discovering threads.
        discover = now - max(window, self._settings.catchup_thread_lookback_sec)

        hist = self._history.read_history(channel, discover, CATCHUP_MAX_THREADS_PER_CHANNEL)
        if hist is None:
            # Can't tell an empty result apart from a Slack-side glitch.
            return Outcome.unknown("기록을 여러 번 읽어도 비어 판정 불가")

        candidates: list[tuple[Mapping[str, Any], str]] = []  # (asking message, its thread)

        for msg in hist:
            ts = str(msg.get("ts") or "")
            thread_ts = str(msg.get("thread_ts") or ts)
            has_thread = bool(msg.get("thread_ts")) or msg.get("reply_count")

            if has_thread:
                latest = float(msg.get("latest_reply") or ts or 0)
                if latest < oldest:
                    continue
                thread = self._history.read_thread(channel, thread_ts, 50)
                if not thread:
                    continue
            else:
                if float(ts or 0) < oldest:
                    continue
                thread = [msg]

            asked: list[Mapping[str, Any]] = []
            bot_in_thread = any(self._identity.is_self(x) for x in thread)
            # Tracks, per human message, whether the bot had just asked a
            # clarifying question right before it.
            asked_before: dict[Any, bool] = {}
            pending_ask = False
            for x in thread:
                if x.get("bot_id"):
                    if self._identity.is_self(x):
                        pending_ask = self._gate.asked_back(self._message_text(x))
                else:
                    asked_before[x.get("ts")] = pending_ask
            for m in thread:
                if not self._kind.is_human(m):
                    continue
                text = m.get("text") or ""
                called = self._identity.is_mentioned(text)
                bot_asked = asked_before.get(m.get("ts"), False)
                # Same rule the live path applies in ResponsePolicy. Without
                # it here, a message addressed to another bot is skipped live
                # and then picked up on the next catch-up round instead.
                if not called and addresses_someone_else(text):
                    continue
                if not called and not (
                    bot_in_thread and self._gate.worth_answering(text, bot_asked)
                ):
                    continue
                if float(m.get("ts", 0)) < oldest:
                    continue
                # A message that just arrived may still be in flight; the
                # grace period only applies to messages from before this
                # process started.
                mts = float(m.get("ts", 0))
                if mts >= self._started_at and mts > now - self._settings.catchup_grace_sec:
                    continue
                if already_handled(m):
                    continue
                asked.append(m)

            for m in unanswered(
                thread, asked, is_self=self._identity.is_self, is_notice=self._notices.is_notice
            ):
                candidates.append((m, thread_ts))

        missed: list[RequestContext] = []
        seen: set[tuple[str, Any]] = set()
        for m, thread_ts in candidates:
            key = (channel, m.get("ts"))
            if key in seen:
                continue
            seen.add(key)
            if self._attachments is not None:
                files, missed_files = self._attachments.download(m)
            else:
                files, missed_files = tuple(m.get("files") or ()), 0
            recovered = RequestContext(
                channel=channel,
                user=m.get("user") or "",
                ts=str(m.get("ts")),
                thread_ts=str(thread_ts),
                text=self._self_mention.remove_self(m.get("text") or ""),
                files=files,
                missed_files=missed_files,
            )
            # The socket path asks back instead of queueing this (sca-yb8q);
            # without the same judgment here the call costs an engine turn
            # anyway, just one outage later (sca-zct1).
            if recovered.has_no_request:
                continue
            missed.append(recovered)
        missed.sort(key=lambda c: float(c.ts))
        return Outcome.found(missed)

    def sweep(self, channels: list[str], window: float) -> CatchupReport:
        """Finds missed requests across all registered channels."""
        missed: list[RequestContext] = []
        skipped: list[RequestContext] = []
        unchecked: list[str] = []

        for channel in channels:
            outcome = self.find_missed(channel, window)
            if outcome.is_unknown:
                unchecked.append(channel)
                continue

            representatives, rest = self._pick_representatives(outcome.value())
            missed.extend(representatives)
            skipped.extend(rest)

        if unchecked:
            self._queue_retry(unchecked)
        else:
            self._pending.clear()

        return CatchupReport(missed=missed, skipped=skipped, unchecked_channels=unchecked)

    def _pick_representatives(
        self, found: list[RequestContext]
    ) -> tuple[list[RequestContext], list[RequestContext]]:
        """Keeps only the newest missed message per thread as a representative.

        The representative's reply restores the full thread context, so
        answering per-message would post a reply for every backlogged message.
        `marked_late()` defers the "sorry for the delay" note to the actual
        reply rather than posting it upfront.
        """
        groups: dict[str, list[RequestContext]] = {}
        for ctx in found:
            groups.setdefault(ctx.thread_ts, []).append(ctx)

        representatives: list[RequestContext] = []
        rest: list[RequestContext] = []
        for items in groups.values():
            items.sort(key=lambda c: float(c.ts))
            representatives.append(items[-1].marked_late())
            rest.extend(items[:-1])
        return representatives, rest

    def retry_pending(self) -> list[RetryStatus]:
        """Re-checks channels whose last catch-up attempt couldn't confirm history."""
        now = self._now()
        due = [
            (ch, first, tries)
            for ch, (first, tries) in self._pending.items()
            if now - first >= CATCHUP_RETRY_BACKOFF[min(tries - 1, len(CATCHUP_RETRY_BACKOFF) - 1)]
        ]

        statuses: list[RetryStatus] = []
        for ch, first, _tries in due:
            outcome = self.find_missed(ch, self._settings.catchup_window_sec)
            if outcome.is_unknown:
                stuck = now - first
                if stuck >= CATCHUP_ALERT_AFTER_SEC:
                    # Alerting itself is the caller's job; this only sets alert=True.
                    self._pending.pop(ch, None)
                else:
                    self._queue_retry([ch])
                statuses.append(
                    RetryStatus(channel=ch, stuck_sec=stuck, alert=stuck >= CATCHUP_ALERT_AFTER_SEC)
                )
                continue

            self._pending.pop(ch, None)
            representatives, _ = self._pick_representatives(outcome.value())
            if representatives:
                statuses.append(
                    RetryStatus(channel=ch, stuck_sec=0.0, alert=False, missed=tuple(representatives))
                )

        return statuses

    def _queue_retry(self, channels: list[str]) -> None:
        now = self._now()
        for ch in channels:
            first, tries = self._pending.get(ch, (now, 0))
            self._pending[ch] = (first, tries + 1)
