"""Figures out who's present in a thread: people who spoke, plus
people pulled in by @mention — a mention notifies them and the thread
is open, so they're treated as present.

Left to infer this on its own, the model treats unmentioned
participants as absent. Counting them explicitly and putting it in
the prompt avoids that.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Protocol

from slack_cli_agent.core.channel_kind import is_direct_message_channel

# Matches Slack's <@U123> or <@U123|name> mention markup.
MENTION_IN_TEXT = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")

# Matches Slack's <!subteam^S123> or <!subteam^S123|@handle> group markup.
GROUP_MENTION_IN_TEXT = re.compile(r"<!subteam\^([A-Z0-9]+)(?:\|[^>]*)?>")

UNKNOWN_NAME = "이름 모르는 사람"
UNKNOWN_GROUP_NAME = "이름 모르는 그룹"


class ThreadHistory(Protocol):
    def read_thread(self, channel: str, thread_ts: str, limit: int) -> list[Any]: ...


class ThreadParticipants:
    def __init__(
        self,
        history: ThreadHistory,
        name_resolver: Callable[[str], str],
        bot_user_id: str,
        limit: int = 40,
        group_resolver: Callable[[str], str] | None = None,
    ) -> None:
        self._history = history
        self._name_resolver = name_resolver
        self._bot_user_id = bot_user_id
        self._limit = limit
        self._group_resolver = group_resolver

    def of(self, channel: str, thread_ts: str) -> tuple[tuple[str, str], ...]:
        """(display name, mention markup) pairs, in first-seen order."""
        # DMs only ever have the bot and one other person — nothing to count.
        if is_direct_message_channel(channel):
            return ()
        try:
            messages = self._history.read_thread(channel, thread_ts, self._limit)
        except Exception:  # noqa: BLE001 - a failed lookup shouldn't fail the request, just omit this section
            return ()

        seen: set[str] = set()
        people: list[tuple[str, str]] = []

        def add(user_id: str | None) -> None:
            if not user_id or user_id in seen or user_id == self._bot_user_id:
                return
            seen.add(user_id)
            people.append((self._name_resolver(user_id) or UNKNOWN_NAME, f"<@{user_id}>"))

        def add_group(group_id: str) -> None:
            # The group goes in as one line rather than its members: expanding
            # a 50-person group would fill the list with it and cost one name
            # lookup per member. The markup is what calls them back (sca-ccyv).
            if not self._group_resolver or not group_id or group_id in seen:
                return
            seen.add(group_id)
            people.append((self._group_resolver(group_id) or UNKNOWN_GROUP_NAME,
                           f"<!subteam^{group_id}>"))

        for message in messages:
            # The bot's own messages aren't a person, but mentions inside them still count.
            if not message.get("bot_id"):
                add(message.get("user"))
            text = message.get("text") or ""
            for user_id in MENTION_IN_TEXT.findall(text):
                add(user_id)
            for group_id in GROUP_MENTION_IN_TEXT.findall(text):
                add_group(group_id)
        return tuple(people)
