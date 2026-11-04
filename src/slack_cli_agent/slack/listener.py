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

from collections.abc import Callable, Mapping
from typing import Any

from slack_cli_agent.config.channel import ChannelRegistry
from slack_cli_agent.core.context import RequestContext
from slack_cli_agent.guard.mentions import calls_other_bot, talking_to_peer
from slack_cli_agent.session.awaiting import AwaitingPort
from slack_cli_agent.slack.gate import ResponseGate
from slack_cli_agent.slack.identity import BotIdentity
from slack_cli_agent.slack.message_kind import MessageKind
from slack_cli_agent.slack.policy import ResponsePolicy, ThreadState


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
    ) -> None:
        self._client = client
        self._channels = channel_registry
        self._gate = gate
        self._awaiting = awaiting
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
        self._is_bot = is_bot or (lambda _user_id: False)

    def _bot_user_id(self, bot_id: str) -> str:
        """The bot user behind a bot_id-only message.

        A bot that posts with chat:write.customize arrives with bot_id and no
        user field, so a controller listed in controller_user_ids was treated
        as a stranger. bots.info maps the bot_id back to its user. A failed
        lookup stays empty, which keeps the old, stricter behavior.
        """
        if not bot_id:
            return ""
        if bot_id not in self._bot_users:
            try:
                info = self._client.bots_info(bot=bot_id)
                self._bot_users[bot_id] = str((info.get("bot") or {}).get("user_id") or "")
            except Exception:  # noqa: BLE001 - a lookup failure (including rate limiting) means no user
                return ""
        return self._bot_users[bot_id]

    def _context_from_event(
        self, event: Mapping[str, Any], *, unaddressed: bool, is_dm: bool
    ) -> RequestContext:
        ts = event.get("ts") or ""
        return RequestContext(
            channel=event.get("channel") or "",
            user=event.get("user") or self._bot_user_id(event.get("bot_id") or ""),
            ts=ts,
            thread_ts=event.get("thread_ts") or ts,
            text=event.get("text") or "",
            files=tuple(event.get("files") or ()),
            unaddressed=unaddressed,
            is_direct_message=is_dm,
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
        if event.get("bot_id") and not self._identity.is_mentioned(event.get("text") or ""):
            return None
        return self._context_from_event(event, unaddressed=False, is_dm=False)

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

    def from_message(self, event: Mapping[str, Any]) -> RequestContext | None:
        if not self._kind.is_human(event):
            return None

        if event.get("channel_type") == "im":
            return self._context_from_event(event, unaddressed=False, is_dm=True)

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
            return None
        has_files = bool(event.get("files"))
        if not self._policy.answers(
            config, text, ThreadState(joined, bot_asked), has_files=has_files
        ):
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

