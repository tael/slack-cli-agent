"""Rewrites Slack mention markup in a message body into readable names.

The speaker label is resolved elsewhere; the body was left as-is, so the
model saw `<@U0EXAMPLE05>` and could not tell who called whom. bot.py:2722
records what that costs -- on 2026-08-26 a reply inverted who said what to
whom (sca-hkmb).

Unresolvable mentions keep their original markup. Dropping them would erase
the fact that someone was called.
"""

from __future__ import annotations

import re
from collections.abc import Callable

#: Slack's <@U123> / <@U123|display name> and <!subteam^S1> / <!subteam^S1|@handle>.
#: One pass over the text: substituting in two passes would let a resolved
#: name that happens to contain markup be rewritten a second time.
MENTION = re.compile(r"<(@|!subteam\^)([A-Z0-9]+)(?:\|([^>]*))?>")

_GROUP_MARK = "!subteam^"


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
        name = self._resolve(resolver, found.group(2)) or (found.group(3) or "").strip()
        if is_group:
            # A handle may or may not carry the @ already; the prefix is added here.
            name = name.lstrip("@")
        if not name:
            return found.group(0)
        return f"@{name} 그룹" if is_group else name

    @staticmethod
    def _resolve(resolver: Callable[[str], str] | None, key: str) -> str:
        if resolver is None:
            return ""
        try:
            return (resolver(key) or "").strip()
        except Exception:  # noqa: BLE001 - a failed lookup leaves the markup, it does not fail the request
            return ""
