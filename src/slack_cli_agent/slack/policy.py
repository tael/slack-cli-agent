"""Decides whether a thread reply that didn't name the bot gets answered.

These criteria used to sit inline in `EventListener.from_message`, where
they were mixed with Slack event parsing. That left no place to vary them
per channel, and no way to test the decision without a fake Slack client.

`considers` is split out from `answers` on purpose: it only reads channel
config, so the caller can skip the `conversations_replies` lookup (a Slack
API call) for channels that never answer unaddressed messages.
"""

from __future__ import annotations

from dataclasses import dataclass

from slack_cli_agent.config.channel import CHAT_DEFAULT, ChannelConfig
from slack_cli_agent.slack.gate import ResponseGate


@dataclass(frozen=True)
class ThreadState:
    """What the thread looks like at decision time."""

    joined: bool
    """Whether the bot has already spoken in this thread."""
    bot_asked: bool
    """Whether the bot's last message in the thread was left awaiting a reply."""


class ResponsePolicy:
    def __init__(self, gate: ResponseGate) -> None:
        self._gate = gate

    def considers(self, config: ChannelConfig | None) -> bool:
        """Whether this channel answers at all without being named.

        An unregistered channel (config is None) never does.
        """
        return bool(config and config.answer_unaddressed)

    def answers(
        self, config: ChannelConfig | None, text: str, thread: ThreadState
    ) -> bool:
        """Whether to answer this particular message.

        The `chat` level moves the bar. `normal` is the default and the
        behaviour every channel had before this setting was read here, so
        a channel that never set `chat` sees no change.

            quiet   only a reply to the bot's own question
            normal  a thread the bot is already in, if the message wants an answer
            active  any thread in the channel, same message filter
        """
        if not self.considers(config):
            return False
        level = config.chat if config else CHAT_DEFAULT
        if level == "quiet":
            return thread.bot_asked
        if level != "active" and not thread.joined:
            return False
        return self._gate.worth_answering(text, thread.bot_asked)
