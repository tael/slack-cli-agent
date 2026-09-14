"""Decides what kind of message a Slack event represents.

Filtering by `subtype`/`bot_id` used to live separately in four places
with different criteria — the socket listener accepted file-attached
messages as new human input, but recovery, addendum collection, and
transcripts all rejected anything with a subtype. A request with a
file attachment was only processed when it arrived live over the
socket; the same request during a restart was lost.

Centralizing this here means there's exactly one place to update the
accepted subtype list.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Messages with an attached file still count as new human input despite having a subtype.
HUMAN_SUBTYPES = frozenset({"file_share"})


class MessageKind:
    def __init__(self, human_subtypes: frozenset[str] = HUMAN_SUBTYPES) -> None:
        self._human_subtypes = human_subtypes

    def is_human(self, msg: Mapping[str, Any]) -> bool:
        """Whether this is new human input (not this bot, not a rejected subtype)."""
        if msg.get("bot_id"):
            return False
        subtype = msg.get("subtype")
        return not subtype or subtype in self._human_subtypes

    def is_transcribable(self, msg: Mapping[str, Any]) -> bool:
        """Whether this belongs in a transcript. Includes bot replies
        — without them, it's unclear what a message was replying to.
        """
        return bool(msg.get("bot_id")) or self.is_human(msg)
