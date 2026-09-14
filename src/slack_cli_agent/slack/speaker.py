"""Builds display names for Slack message speakers, in one place.

TranscriptBuilder._speaker_of and LateAddendumChecker._speaker_of used
to each carry the same logic separately — when a naming rule gets
added to one but not the other, the two views of the same conversation
disagree. Both call sites now delegate here.

late_addendum's caller pre-filters with MessageKind.is_human, so no
bot message (this bot's or another's) ever reaches speaker_of there.
That's why passing a function that always returns False for is_self
still produces the same output there today — the bot branch exists in
this code, but nothing currently reaches it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


class SpeakerNamer:
    def __init__(
        self,
        name_resolver: Callable[[str], str],
        is_self: Callable[[Mapping[str, Any]], bool],
        bot_display_name: str,
        owner_user_id: str,
        owner_display_name: str,
    ) -> None:
        self._name_resolver = name_resolver
        self._is_self = is_self
        self._bot_display_name = bot_display_name
        self._owner_user_id = owner_user_id
        self._owner_display_name = owner_display_name

    def speaker_of(self, msg: Mapping[str, Any]) -> str:
        if self._is_self(msg):
            return self._bot_display_name or "봇"
        if msg.get("bot_id"):
            profile = msg.get("bot_profile") or {}
            name = profile.get("name") or msg.get("username") or ""
            return f"{name} (다른 봇)" if name else "이름 모르는 봇"
        user = msg.get("user") or ""
        if user == self._owner_user_id and self._owner_display_name:
            return self._owner_display_name
        name = self._name_resolver(user) or "이름 모르는 사람"
        return f"{name} <@{user}>" if user else name
