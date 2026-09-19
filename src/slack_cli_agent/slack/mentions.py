"""Rewrites Slack mention markup in a message body into readable names.

The speaker label is resolved elsewhere; the body was left as-is, so the
model saw `<@U0ACANBHY3U>` and could not tell who called whom. bot.py:2722
records what that costs -- on 2026-08-26 a reply inverted who said what to
whom (sca-hkmb).

Unresolvable mentions keep their original markup. Dropping them would erase
the fact that someone was called.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

#: Slack's <@U123> / <@U123|display name> and <!subteam^S1> / <!subteam^S1|@handle>.
#: One pass over the text: substituting in two passes would let a resolved
#: name that happens to contain markup be rewritten a second time.
MENTION = re.compile(r"<(@|!subteam\^)([A-Z0-9]+)(?:\|([^>]*))?>")

_GROUP_MARK = "!subteam^"

#: <!here>, <!channel>, <!everyone>. These call people the mention markup
#: never names, so the transcript head is the only place they show up.
BROADCAST = re.compile(r"<!(here|channel|everyone)(?:\|[^>]*)?>")

BROADCAST_NAMES = {"here": "이 자리 모두"}
BROADCAST_DEFAULT = "채널 모두"

#: Fallback for an unresolved identity. Only the leading mention: on the
#: events this runs on, that position is this bot. Removing every mention
#: would throw away who else was called, which is what this class exists to
#: keep (codex review).
_LEADING_MENTION = re.compile(r"^\s*<@[^>\s]+>")


def resolve_name(resolver: Callable[[str], str] | None, key: str) -> str:
    """A failed lookup leaves the markup, it does not fail the request."""
    if resolver is None:
        return ""
    try:
        return (resolver(key) or "").strip()
    except Exception:  # noqa: BLE001 - see docstring
        return ""


class MentionRenderer:
    def __init__(
        self,
        name_resolver: Callable[[str], str],
        group_resolver: Callable[[str], str] | None = None,
    ) -> None:
        self._name = name_resolver
        self._group = group_resolver

    def render(self, text: str) -> str:
        if not text:
            return text
        return MENTION.sub(self._one, text)

    def _one(self, found: re.Match[str]) -> str:
        is_group = found.group(1) == _GROUP_MARK
        resolver = self._group if is_group else self._name
        name = resolve_name(resolver, found.group(2)) or (found.group(3) or "").strip()
        if is_group:
            # A handle may or may not carry the @ already; the prefix is added here.
            name = name.lstrip("@")
        if not name:
            return found.group(0)
        return f"@{name} 그룹" if is_group else name


class SelfMentionStripper:
    """Removes this bot's own mention from an incoming message.

    Every mention used to go with it (bot.py:4973), so the body reaching the
    model no longer said who had been called. Other people's mentions are kept
    here and turned into names where the prompt is built (sca-za2a).

    An unresolved identity falls back to removing the leading mention only:
    this text is what the admin router matches, and a leading `<@U...>` stops
    every admin command. Mentions further in keep their markup.
    """

    def __init__(self, identity: Any) -> None:
        self._identity = identity

    def remove_self(self, text: str) -> str:
        if not text:
            return text
        return self._pattern().sub("", text).strip()

    def _pattern(self) -> re.Pattern[str]:
        """Built from the id itself rather than from MENTION: an id that the
        general pattern does not match would leave this bot's own mention in
        the body, and no admin command would match it."""
        user_id = self._self_user_id()
        if not user_id:
            return _LEADING_MENTION
        return re.compile(rf"<@{re.escape(user_id)}(?:\|[^>]*)?>")

    def _self_user_id(self) -> str:
        try:
            return str(self._identity.user_id or "")
        except Exception:  # noqa: BLE001 - a lookup failure falls back to removing every mention
            return ""


class CalledNames:
    """Collects who a message called, for the transcript head.

    The body itself stays as Slack sent it. Substituting names into it made
    the model copy that form into its own reply, and a bare name does not
    link -- the person called never finds out (bot.py:2722, incidents on
    2026-08-26 12:02 and 12:05). The original carries the direction in the
    line head instead: `[time speaker -> called]` (bot.py:2874).

    Users first, then groups, then broadcasts -- the original's scan order,
    kept so the same conversation reads the same way here.
    """

    def __init__(
        self,
        name_resolver: Callable[[str], str],
        group_resolver: Callable[[str], str] | None = None,
    ) -> None:
        self._name = name_resolver
        self._group = group_resolver

    def called_in(self, text: str) -> tuple[str, ...]:
        """Deduplicates by who was called, not by the name shown. Two people
        who share a display name are two people, and `<!channel>` next to
        `<!everyone>` is two calls -- the original counts ids and keywords
        (bot.py:2733, 2749)."""
        if not text:
            return ()
        users: list[tuple[str, str]] = []
        groups: list[tuple[str, str]] = []
        for match in MENTION.finditer(text):
            key = match.group(2)
            if match.group(1) == _GROUP_MARK:
                # The handle written into the markup is what the original
                # falls back to when the group lookup fails (bot.py:2707).
                name = (resolve_name(self._group, key) or (match.group(3) or "")).strip().lstrip("@")
                groups.append((key, f"@{name} 그룹" if name else ""))
            else:
                users.append((key, resolve_name(self._name, key)))
        broadcasts = [
            (word, BROADCAST_NAMES.get(word, BROADCAST_DEFAULT))
            for word in (m.group(1) for m in BROADCAST.finditer(text))
        ]

        found: list[str] = []
        seen: set[str] = set()
        for key, name in users + groups + broadcasts:
            if name and key not in seen:
                seen.add(key)
                found.append(name)
        return tuple(found)
