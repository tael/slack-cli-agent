"""Catch-up: recovers mentions that arrived while the socket was disconnected.

A persistent job queue handles other restart-recovery cases, but messages
that arrive during a socket outage never produce an event to queue in the
first place — the only way to recover them is to re-read Slack history.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from ..config.settings import RuntimeSettings
from ..core.context import RequestContext
from ..core.result import Outcome
from ..guard.mentions import PeerExchangeLimiter, calls_other_bot, talking_to_peer
from ..observability.notices import NoticeCatalog
from ..slack.attachments import AttachmentStore
from ..slack.blocks import event_body
from ..slack.gate import ResponseGate
from ..slack.identity import BotIdentity
from ..slack.mentions import SelfMentionStripper
from ..slack.message_kind import HUMAN_SUBTYPES, MessageKind
from ..slack.policy import addresses_someone_else
from .ports import HistoryReader

_UNKNOWN_IDENTITY_REASON = "봇 신원을 몰라 부름 판정 불가"

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
        if x.get("bot_id") and x.get("ts") not in waiting:
            continue  # another bot's message; not an ask to us or our reply
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


class StuckCause:
    """Why a channel stayed on the retry list, so the owner alert can say so."""

    EMPTY_HISTORY = "empty_history"
    """Slack kept returning an empty channel history."""
    UNKNOWN_IDENTITY = "unknown_identity"
    """The bot's own identity is unknown, so mentions can't be judged."""
    UNREADABLE_THREAD = "unreadable_thread"
    """Some thread couldn't be read to its last page (failed page or page cap)."""


@dataclass(frozen=True)
class RetryStatus:
    """Status `retry_pending` returns for one channel."""

    channel: str
    stuck_sec: float
    alert: bool
    missed: tuple[RequestContext, ...] = ()
    cause: str = StuckCause.EMPTY_HISTORY
    """One of StuckCause; picks the owner alert's explanation."""
    unreadable_threads: int = 0
    """Threads left undecided this round when cause is UNREADABLE_THREAD."""


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
        is_bot: Callable[[str], bool] = lambda _user_id: False,
        bot_user_of: Callable[[str], str] = lambda _bot_id: "",
        peer_limiter: PeerExchangeLimiter | None = None,
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
        # Same lookup the listener's calls_other_bot/talking_to_peer use
        # (C16, C17). The default treats nobody as a bot, which keeps the
        # older behavior for a caller that doesn't wire it.
        self._is_bot = is_bot
        # bots.info lookup for a bot_id-only post, the same correction the
        # live path makes (listener._bot_user_id). Without it a recovered peer
        # request had an empty asker: no controller trust, asker_is_bot False,
        # and AddresseeGuard stripping the mention the peer needs (C3).
        self._bot_user_of = bot_user_of
        # The listener's bot-to-bot cap (C19), shared. A peer mention the live
        # path dropped over the cap gets no mark, so without this the next
        # catch-up answered it anyway (mametchi bot.py peer_turn_allowed).
        self._peer_limiter = peer_limiter

    #: Subtypes a peer bot's request can carry (listener._BOT_POST_SUBTYPES).
    _BOT_POST_SUBTYPES = frozenset({"bot_message"}) | HUMAN_SUBTYPES

    def _peer_request(self, msg: Mapping[str, Any]) -> bool:
        """Whether another bot named this bot in this message (C3).

        The live path takes these through app_mention (C26). Catch-up used to
        drop every bot-authored message, so a peer's call cut off by a restart
        was never answered: no human is in that thread to ask again
        (mametchi bot.py `addressed_by_bot`).
        """
        if not msg.get("bot_id") or self._identity.is_self(msg):
            return False
        subtype = msg.get("subtype")
        if subtype and subtype not in self._BOT_POST_SUBTYPES:
            return False
        return self._identity.is_mentioned(event_body(msg))

    def find_missed(self, channel: str, window: float) -> Outcome[list[RequestContext]]:
        """Finds mentions that never got a reply, including thread replies.

        Unknown when any thread in the window could not be read in full;
        sweep and retry_pending use _scan to keep the rest of the channel.
        """
        outcome, undecided = self._scan(channel, window)
        if outcome.is_found and undecided:
            return Outcome.unknown("스레드를 끝까지 읽지 못해 판정 불가")
        return outcome

    def _scan(self, channel: str, window: float) -> tuple[Outcome[list[RequestContext]], int]:
        """Returns the missed mentions found and how many threads were undecidable.

        One unreadable thread (a failed later page, or the page cap on a long
        thread) must not throw away the rest of the channel. Returning early
        would leave every other thread's missed mention unrecovered until the
        retry gives up with an alert (PR #124 review). The caller still keeps
        the channel on the retry list while the count is non-zero.
        """
        if not self._identity.known:
            # Without identity we can't judge what counts as a mention. Returning
            # an empty list here would look the same as "nothing missed" and this
            # round would drop out of retry, so the gap never gets recovered.
            return Outcome.unknown(_UNKNOWN_IDENTITY_REASON), 0

        now = self._now()
        oldest = now - window
        # A thread's parent can be old while a reply just landed, so look back
        # further than the catch-up window itself when discovering threads.
        discover = now - max(window, self._settings.catchup_thread_lookback_sec)

        hist = self._history.read_history(channel, discover, CATCHUP_MAX_THREADS_PER_CHANNEL)
        if hist is None:
            # Can't tell an empty result apart from a Slack-side glitch.
            return Outcome.unknown("기록을 여러 번 읽어도 비어 판정 불가"), 0

        # (asking message, its thread, was the bot directly mentioned)
        candidates: list[tuple[Mapping[str, Any], str, bool]] = []
        undecided = 0

        for msg in hist:
            ts = str(msg.get("ts") or "")
            thread_ts = str(msg.get("thread_ts") or ts)
            has_thread = bool(msg.get("thread_ts")) or msg.get("reply_count")

            if has_thread:
                latest = float(msg.get("latest_reply") or ts or 0)
                if latest < oldest:
                    continue
                full = self._history.read_full_thread(channel, thread_ts, 50)
                if full is None:
                    # A truncated thread may be missing the bot's latest
                    # reply, so it can't be judged. Count it so the channel
                    # stays on the retry list, and keep scanning the others.
                    undecided += 1
                    continue
                if not full:
                    continue
                thread = full
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
                peer = self._peer_request(m)
                if not peer and not self._kind.is_human(m):
                    continue
                # The body, not `text`: a rich post's `text` is only its
                # notification preview, so a mention mid-body went unseen (C3).
                text = event_body(m)
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
                # A message naming another bot anywhere is that bot's (C16),
                # and so is an unnamed follow-up after a human last addressed
                # another bot (C17). The live path skips both; without these
                # the next catch-up round answered them right after a restart.
                if not called and calls_other_bot(self._is_bot, self._identity.user_id, text):
                    continue
                if (
                    not called
                    and not bot_asked
                    and talking_to_peer(
                        thread,
                        is_bot=self._is_bot,
                        self_id=self._identity.user_id,
                        upto_ts=str(m.get("ts") or ""),
                    )
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
                called = self._identity.is_mentioned(event_body(m))
                candidates.append((m, thread_ts, called))

        missed: list[RequestContext] = []
        seen: set[tuple[str, Any]] = set()
        for m, thread_ts, called in candidates:
            key = (channel, m.get("ts"))
            if key in seen:
                continue
            seen.add(key)
            bot_id = str(m.get("bot_id") or "")
            recovered = RequestContext(
                channel=channel,
                user=m.get("user") or self._bot_user_of(bot_id),
                ts=str(m.get("ts")),
                thread_ts=str(thread_ts),
                # The whole body; a rich post's `text` is a one-line preview.
                text=self._self_mention.remove_self(event_body(m)),
                # Raw event files carried through untouched; downloading here
                # would fetch every candidate's attachment even though only
                # the thread's representative survives _pick_representatives
                # (코덱스 8차 리뷰, sca-b8gn).
                files=tuple(m.get("files") or ()),
                missed_files=0,
                # Mirrors listener.py's from_app_mention (mentioned ->
                # unaddressed=False) vs from_message (thread continuation
                # without a mention -> True). Left unset, SilenceRuleSection
                # never applies to a recovered continuation (코덱스 8차
                # 리뷰, sca-f6ii).
                unaddressed=not called,
                # Marks a peer's request so the bot-to-bot cap can count it
                # in _pick_representatives, as bot.py keeps bot_id (C3).
                extra={"bot_id": bot_id} if bot_id else {},
            )
            # The socket path asks back instead of queueing this (sca-yb8q);
            # without the same judgment here the call costs an engine turn
            # anyway, just one outage later (sca-zct1).
            if recovered.has_no_request:
                continue
            missed.append(recovered)
        missed.sort(key=lambda c: float(c.ts))
        return Outcome.found(missed), undecided

    def sweep(self, channels: list[str], window: float) -> CatchupReport:
        """Finds missed requests across all registered channels."""
        missed: list[RequestContext] = []
        skipped: list[RequestContext] = []
        unchecked: list[str] = []

        for channel in channels:
            outcome, undecided = self._scan(channel, window)
            if outcome.is_unknown:
                unchecked.append(channel)
                continue

            representatives, rest = self._pick_representatives(outcome.value())
            missed.extend(representatives)
            skipped.extend(rest)
            if undecided:
                unchecked.append(channel)

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
            if not self._peer_turn_allowed(items):
                rest.extend(items)
                continue
            representatives.append(self._with_attachments(items[-1]).marked_late())
            rest.extend(items[:-1])
        return representatives, rest

    def _peer_turn_allowed(self, items: list[RequestContext]) -> bool:
        """Applies the live path's bot-to-bot cap to one thread's backlog.

        Any human candidate starts the count over (C18), named or not, as
        listener.from_message resets on every human thread turn; narrowing
        it to named turns would drop the human's own unnamed question along
        with the capped peer request. The peer request about to be answered
        then counts one turn (C19). Only the representative counts: it is
        the one turn actually answered.
        """
        if self._peer_limiter is None:
            return True
        thread = items[-1].thread_ts
        if any(not c.extra.get("bot_id") for c in items):
            self._peer_limiter.reset(thread)
        if not items[-1].extra.get("bot_id"):
            return True
        return self._peer_limiter.allowed(thread, getattr(items[-1], "channel", "") or "")

    def _with_attachments(self, ctx: RequestContext) -> RequestContext:
        """Downloads attachments only for the representative a thread group
        actually keeps, not every candidate find_missed produced (sca-b8gn)."""
        if self._attachments is None or not ctx.files:
            return ctx
        files, missed_files = self._attachments.download({"ts": ctx.ts, "files": list(ctx.files)})
        return replace(ctx, files=files, missed_files=missed_files)

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
            outcome, undecided = self._scan(ch, self._settings.catchup_window_sec)
            if outcome.is_unknown or undecided:
                # Missed mentions in the readable threads are recovered now,
                # not held back until the unreadable one clears or alerts.
                found = outcome.value() if outcome.is_found else []
                representatives, _ = self._pick_representatives(found)
                stuck = now - first
                if stuck >= CATCHUP_ALERT_AFTER_SEC:
                    # Alerting itself is the caller's job; this only sets alert=True.
                    self._pending.pop(ch, None)
                else:
                    self._queue_retry([ch])
                if outcome.is_found:
                    cause = StuckCause.UNREADABLE_THREAD
                elif outcome.reason == _UNKNOWN_IDENTITY_REASON:
                    cause = StuckCause.UNKNOWN_IDENTITY
                else:
                    cause = StuckCause.EMPTY_HISTORY
                statuses.append(
                    RetryStatus(
                        channel=ch,
                        stuck_sec=stuck,
                        alert=stuck >= CATCHUP_ALERT_AFTER_SEC,
                        missed=tuple(representatives),
                        cause=cause,
                        unreadable_threads=undecided,
                    )
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
