"""Turns a raw Slack event into a RequestContext, or decides it's not
one to act on.

- app_mention: always accepted
- message: DMs always accepted. In channels, only accepted for thread
  replies where the bot already has a foot in the thread — a mention
  is already handled by app_mention, so it isn't processed twice here
- reaction_added: only accepted when a registered emoji lands on this
  bot's own reply

Whether an unaddressed thread reply gets answered is decided by
`ResponsePolicy`, not here — this class only supplies the facts it needs
(the channel config and the thread state).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

from slack_cli_agent.config.channel import ChannelRegistry
from slack_cli_agent.core.context import RequestContext
from slack_cli_agent.observability.audit import IncidentKind
from slack_cli_agent.guard.mentions import (
    PeerExchangeLimiter,
    calls_other_bot,
    talking_to_peer,
)
from slack_cli_agent.session.awaiting import AwaitingPort
from slack_cli_agent.slack.blocks import event_body
from slack_cli_agent.slack.gate import ResponseGate
from slack_cli_agent.slack.identity import BotIdentity
from slack_cli_agent.slack.message_kind import HUMAN_SUBTYPES, MessageKind
from slack_cli_agent.slack.names import DEFAULT_BOT_RETRY_INTERVAL_SEC
from slack_cli_agent.slack.policy import ResponsePolicy, ThreadState

log = logging.getLogger(__name__)


class EventListener:
    def __init__(
        self,
        client: Any,
        channel_registry: ChannelRegistry,
        gate: ResponseGate,
        identity: BotIdentity,
        # The persisted "waiting on a decision" flag. The live asked_back
        # check below only sees the posted body, which has the `[[ASK]]` tag
        # stripped — a turn that signalled only through the tag (neutral
        # phrasing, no question suffix) would be missed without this, and the
        # flag also survives a restart. None falls back to the live check
        # alone, which keeps existing callers and tests working.
        awaiting: AwaitingPort | None = None,
        # Whether a user ID belongs to a bot (C16/C17/C26). None keeps the
        # old behavior exactly — nobody is ever a bot, so talking_to_peer
        # never suppresses an unaddressed reply and from_app_mention's extra
        # self-mention check below is the only new filtering a caller that
        # doesn't pass this gets.
        is_bot: Callable[[str], bool] | None = None,
        # Caps a bot-to-bot back-and-forth in one thread (C19), reset the
        # moment a human speaks (C18). None keeps the old behavior: nothing
        # caps the exchange here, and the only brake on a loop is the
        # outgoing BotMentionGuard stripping the asker's mention (sca-c4m).
        peer_limiter: PeerExchangeLimiter | None = None,
        # How long a bot_id whose bots.info lookup failed twice in a row
        # stays un-retried (C5). Same interval as BotUserResolver.
        clock: Callable[[], float] = time.monotonic,
        bot_user_retry_interval_sec: float = DEFAULT_BOT_RETRY_INTERVAL_SEC,
        # Writes the peer-cap and skip rows (G7, mametchi peer_turn_limit,
        # peer_skipped, gate_skipped). None records nothing.
        audit: Callable[..., None] | None = None,
    ) -> None:
        self._client = client
        self._channels = channel_registry
        self._gate = gate
        self._awaiting = awaiting
        self._peer_limiter = peer_limiter
        self._audit = audit or (lambda *_a, **_k: None)
        # Self-message detection and mention matching must use the
        # same identity source — holding it separately would let
        # wiring pass the value to only one and still pass unit tests.
        self._identity = identity
        # Message classification lives in one place; duplicating it
        # here would let the criteria drift.
        self._kind = MessageKind()
        # Whether to speak up without being named is a channel-level
        # decision, kept out of this class so it can vary per channel.
        self._policy = ResponsePolicy(gate)
        self._bot_users: dict[str, str] = {}
        self._bot_user_failed_at: dict[str, float] = {}
        self._clock = clock
        self._bot_user_retry_interval_sec = bot_user_retry_interval_sec
        self._is_bot = is_bot or (lambda _user_id: False)

    def _bot_user_id(self, bot_id: str) -> str:
        """The bot user behind a bot_id-only message.

        A bot that posts with chat:write.customize arrives with bot_id and no
        user field, so a controller listed in controller_user_ids was treated
        as a stranger. bots.info maps the bot_id back to its user.

        An empty user drops the asker mention from the reply, so the bot that
        called never sees the answer (C5, bot.py:4371 bot_user_id_of). A
        failure is therefore asked once more on the spot, and if that fails
        too it is throttled, not cached: the next event after the retry
        interval asks again. A resolved user is cached for the process.
        """
        if not bot_id:
            return ""
        if bot_id in self._bot_users:
            return self._bot_users[bot_id]
        last = self._bot_user_failed_at.get(bot_id)
        if last is not None and self._clock() - last < self._bot_user_retry_interval_sec:
            return ""
        for _attempt in range(2):
            try:
                info = self._client.bots_info(bot=bot_id)
            except Exception as exc:  # noqa: BLE001 - a lookup failure (including rate limiting) is retried, not raised
                log.warning("봇 사용자 ID 조회 실패 : %s : %s", bot_id, exc)
                continue
            user_id = str((info.get("bot") or {}).get("user_id") or "")
            self._bot_users[bot_id] = user_id
            self._bot_user_failed_at.pop(bot_id, None)
            return user_id
        self._bot_user_failed_at[bot_id] = self._clock()
        return ""

    def _context_from_event(
        self, event: Mapping[str, Any], *, unaddressed: bool, is_dm: bool
    ) -> RequestContext:
        ts = event.get("ts") or ""
        return RequestContext(
            channel=event.get("channel") or "",
            user=event.get("user") or self._bot_user_id(event.get("bot_id") or ""),
            ts=ts,
            thread_ts=event.get("thread_ts") or ts,
            # A peer bot's rich post carries only a one-line notification
            # preview in `text`; its tables and lists are in the blocks (C2,
            # bot.py swaps in event_body for a bot sender). A human's `text` is
            # the whole message with its formatting, so it is kept as is.
            text=(event_body(event) if event.get("bot_id") else event.get("text")) or "",
            files=tuple(event.get("files") or ()),
            unfurl_attachments=tuple(event.get("attachments") or ()),
            unaddressed=unaddressed,
            is_direct_message=is_dm,
            from_bot=bool(event.get("bot_id")),
        )

    def from_app_mention(self, event: Mapping[str, Any]) -> RequestContext | None:
        # No bot_id filter here, unlike from_message. The original bot.py:5410
        # does the same, and test-channel-probe wakes a bot with another bot's
        # token through this path. Loop risk is handled on the outgoing side
        # (sca-c4m), not by rejecting here.
        #
        # A bot-authored event still has to actually name this bot (C26,
        # bot.py's `addressed_by_bot`). Slack's own app_mention trigger
        # guarantees that for anything arriving straight off the socket, but
        # this method has no way to tell that case apart from one replayed
        # or reconstructed without going through Slack's trigger — and
        # answering another bot's cross-talk that never named this bot is
        # exactly the kind of loop C19's cap exists to stop from running
        # away in the first place.
        if event.get("bot_id"):
            # The body, not just `text`: a rich post's `text` is only its
            # notification preview, and the mention may sit in its blocks (C1).
            if not self._identity.is_mentioned(event_body(event)):
                return None
            # A peer named this bot: count the turn and stop once the thread's
            # bot-to-bot exchange has gone past its cap (C19). Counting here,
            # at the only entry a bot-authored mention takes, mirrors bot.py
            # incrementing right before it answers a bot.
            if self._peer_limiter is not None and not self._peer_limiter.allowed(
                self._thread_key(event)
            ):
                self._audit(
                    IncidentKind.PEER_TURN_LIMIT.value, channel=event.get("channel") or "",
                    thread_ts=self._thread_key(event), message_ts=event.get("ts") or "",
                )
                return None
        elif self._peer_limiter is not None:
            # A human named this bot: the thread's bot-exchange count starts
            # over (C18), so a later peer turn is answered again.
            self._peer_limiter.reset(self._thread_key(event))
        return self._context_from_event(event, unaddressed=False, is_dm=False)

    def _audit_skip(self, kind: IncidentKind, event: Mapping[str, Any], bot_asked: bool) -> None:
        self._audit(
            kind.value, channel=event.get("channel") or "",
            thread_ts=str(event.get("thread_ts") or ""), user=event.get("user") or "",
            message_ts=event.get("ts") or "", question=(event.get("text") or "")[:200],
            bot_asked=bot_asked,
        )

    @staticmethod
    def _thread_key(event: Mapping[str, Any]) -> str:
        """The thread a peer exchange is counted against — its root when the
        message is a reply, else the message's own ts (the root it opens)."""
        return str(event.get("thread_ts") or event.get("ts") or "")

    def _thread_state(self, channel: str, thread_ts: str) -> tuple[bool, bool, bool, bool]:
        """(joined, bot_asked, talking_to_peer, root_self) — bot.py:8336-8367's
        4-tuple (E6).

        `joined`: has the bot already spoken in this thread.
        `bot_asked`: was its last message there left awaiting a reply.
        `talking_to_peer`: did a human's last address in this thread name a
        different bot, not this one (C17).
        `root_self`: did this bot open the thread (its message is the root).

        The first two feed `slack.policy.ThreadState`; the other two are
        read directly by `from_message` rather than being routed through
        that dataclass — it belongs to a file this bundle doesn't own.
        """
        try:
            replies = self._client.conversations_replies(
                channel=channel, ts=thread_ts, limit=30
            )
        except Exception:  # noqa: BLE001 - treat a lookup failure (including rate limiting) as not joined
            return False, False, False, False
        msgs = replies.get("messages", [])
        joined = any(self._identity.is_self(m) for m in msgs)
        last_self = next((m for m in reversed(msgs) if self._identity.is_self(m)), None)
        asked = self._gate.asked_back(last_self.get("text") if last_self else "")
        # The live check reads the posted body, which no longer carries the
        # `[[ASK]]` tag; fall back to the persisted flag for a turn that
        # asked only through the tag, or whose question predates a restart.
        if not asked and self._awaiting is not None:
            asked = self._awaiting.is_awaiting(channel, thread_ts)
        peer_talk = talking_to_peer(msgs, is_bot=self._is_bot, self_id=self._identity.user_id)
        root_self = bool(msgs) and msgs[0].get("ts") == thread_ts and self._identity.is_self(msgs[0])
        return joined, asked, peer_talk, root_self

    #: Subtypes a bot's own post can carry and still be a request. Edits,
    #: deletions and join notices are not; only these pass for a bot.
    _BOT_POST_SUBTYPES = frozenset({"bot_message"})

    def _named_only_in_blocks(self, event: Mapping[str, Any]) -> bool:
        """Whether this bot is named in the message's blocks but not in its
        `text` (C1).

        Slack derives app_mention from `text`, so a rich post whose mention
        sits only in its blocks raises no app_mention, and the request was
        never taken. A mention in `text` is left to app_mention, so the same
        message is not taken twice.
        """
        if not event.get("blocks") or self._identity.is_self(event):
            return False
        if event.get("bot_id"):
            subtype = event.get("subtype")
            if subtype and subtype not in self._BOT_POST_SUBTYPES | HUMAN_SUBTYPES:
                return False
        elif not self._kind.is_human(event):
            return False
        return not self._identity.is_mentioned(
            event.get("text") or ""
        ) and self._identity.is_mentioned(event_body(event))

    def from_message(self, event: Mapping[str, Any]) -> RequestContext | None:
        if event.get("channel_type") != "im" and self._named_only_in_blocks(event):
            # Taken as the mention it is, through the same checks app_mention
            # gets, including the bot-to-bot cap (C19) and its reset (C18).
            return self.from_app_mention(event)
        if not self._kind.is_human(event):
            return None

        if event.get("channel_type") == "im":
            return self._context_from_event(event, unaddressed=False, is_dm=True)

        # A human spoke in a channel thread: reset the bot-exchange cap (C18)
        # whether or not this turn is one the bot answers. Done before the
        # early returns below so a filtered human turn still clears the count.
        if self._peer_limiter is not None:
            self._peer_limiter.reset(self._thread_key(event))

        channel = event.get("channel") or ""
        thread_ts = event.get("thread_ts")
        text = event.get("text") or ""
        if not thread_ts:
            # A top-level message named nobody. Normally that's not ours, but a
            # channel can opt in with `answer_top_level` (mametchi bot.py:10089).
            # A self-mention is app_mention's to handle; a message naming
            # another bot is that bot's. Treat it as unaddressed either way.
            config = self._channels.get(channel)
            if (
                self._policy.answers_top_level(config)
                and not self._identity.is_mentioned(text)
                and not calls_other_bot(self._is_bot, self._identity.user_id, text)
            ):
                return self._context_from_event(event, unaddressed=True, is_dm=False)
            return None

        if self._identity.is_mentioned(text):
            # app_mention already handles mentions; don't process this twice.
            return None

        # A message naming another bot anywhere in its text is that bot's to
        # answer (C16), not just one with a leading mention — ResponsePolicy's
        # `addresses_someone_else` only catches the leading-mention form.
        if calls_other_bot(self._is_bot, self._identity.user_id, text):
            return None

        config = self._channels.get(channel)
        # Checked before the thread lookup below, which costs a Slack API call.
        if not self._policy.considers(config):
            return None

        joined, bot_asked, peer_talk, _root_self = self._thread_state(channel, thread_ts)
        # A human's last address in this thread named a different bot (C17).
        # Skipped only when this bot itself isn't the one being followed up
        # with — a reply to this bot's own question still belongs to it even
        # if an earlier turn addressed a peer.
        if peer_talk and not bot_asked:
            self._audit_skip(IncidentKind.PEER_SKIPPED, event, bot_asked)
            return None
        has_files = bool(event.get("files"))
        if not self._policy.answers(
            config, text, ThreadState(joined, bot_asked), has_files=has_files
        ):
            # Only where the bot already spoke: elsewhere every passing
            # remark in a registered channel would get a row.
            if joined:
                self._audit_skip(IncidentKind.GATE_SKIPPED, event, bot_asked)
            return None

        return self._context_from_event(event, unaddressed=True, is_dm=False)

    def from_reaction(
        self, event: Mapping[str, Any], allowed: frozenset[str]
    ) -> tuple[str, str, str, str] | None:
        """Extracts (emoji, channel, message ts, reactor) from a
        reaction event, or None if it's not one to act on.
        """
        reaction = event.get("reaction")
        if reaction not in allowed:
            return None
        item = event.get("item") or {}
        if item.get("type") != "message":
            return None
        # Fails closed while identity is unknown: with nothing to compare
        # item_user against, skipping the check would accept an emoji on any
        # message, wider than "this bot's own reply" (sca-8o5f).
        if not self._identity.user_id:
            return None
        if event.get("item_user") != self._identity.user_id:
            return None
        if event.get("user") == self._identity.user_id:
            return None
        return reaction, item.get("channel") or "", item.get("ts") or "", event.get("user") or ""

