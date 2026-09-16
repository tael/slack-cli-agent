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

from collections.abc import Mapping
from typing import Any

from slack_cli_agent.config.channel import ChannelRegistry
from slack_cli_agent.core.context import RequestContext
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
    ) -> None:
        self._client = client
        self._channels = channel_registry
        self._gate = gate
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

    def _context_from_event(
        self, event: Mapping[str, Any], *, unaddressed: bool, is_dm: bool
    ) -> RequestContext:
        ts = event.get("ts") or ""
        return RequestContext(
            channel=event.get("channel") or "",
            user=event.get("user") or "",
            ts=ts,
            thread_ts=event.get("thread_ts") or ts,
            text=event.get("text") or "",
            files=tuple(event.get("files") or ()),
            unaddressed=unaddressed,
            is_direct_message=is_dm,
        )

    def from_app_mention(self, event: Mapping[str, Any]) -> RequestContext:
        return self._context_from_event(event, unaddressed=False, is_dm=False)

    def _thread_state(self, channel: str, thread_ts: str) -> tuple[bool, bool]:
        """(has the bot already joined this thread, was its last message a question)."""
        try:
            replies = self._client.conversations_replies(
                channel=channel, ts=thread_ts, limit=30
            )
        except Exception:  # noqa: BLE001 - treat a lookup failure (including rate limiting) as not joined
            return False, False
        msgs = replies.get("messages", [])
        joined = any(self._identity.is_self(m) for m in msgs)
        last_self = next((m for m in reversed(msgs) if self._identity.is_self(m)), None)
        asked = self._gate.asked_back(last_self.get("text") if last_self else "")
        return joined, asked

    def from_message(self, event: Mapping[str, Any]) -> RequestContext | None:
        if not self._kind.is_human(event):
            return None

        if event.get("channel_type") == "im":
            return self._context_from_event(event, unaddressed=False, is_dm=True)

        channel = event.get("channel") or ""
        thread_ts = event.get("thread_ts")
        if not thread_ts:
            return None

        text = event.get("text") or ""
        if self._identity.is_mentioned(text):
            # app_mention already handles mentions; don't process this twice.
            return None

        config = self._channels.get(channel)
        # Checked before the thread lookup below, which costs a Slack API call.
        if not self._policy.considers(config):
            return None

        joined, bot_asked = self._thread_state(channel, thread_ts)
        if not self._policy.answers(config, text, ThreadState(joined, bot_asked)):
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
        if self._identity.user_id and event.get("item_user") != self._identity.user_id:
            return None
        if self._identity.user_id and event.get("user") == self._identity.user_id:
            return None
        return reaction, item.get("channel") or "", item.get("ts") or "", event.get("user") or ""

