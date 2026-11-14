"""Fixes outgoing links before they reach Slack.

`fix_notion_links` restores the missing `p/` in app.notion.com page links.
Notion's canonical page URL path starts with `p/`; without it the link is a
404, with or without a workspace name in front of the id. The SQL mode of
notion-query-data-sources returns its url column in that broken shape, so
copying a query result verbatim posts a dead link. Readers only find out by
clicking, and a posted link can't be taken back, so it is fixed here.

MessagePublisher.post/update call this on every outgoing body, the same
place mametchi's bot.py post() does it. Doing it in the guard pipeline
missed every path that posts without it: watch reports, review results,
and the engine's user-facing failure body.

Only links whose path ends in a page id are touched. Adding `p/` to other
paths (login, install) would break those instead.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

NOTION_BAD_LINK = re.compile(r'https?://app\.notion\.com/(?!p/)([^\s"\'<>)\]|]+)')
NOTION_PAGE_ID = re.compile(
    r"(?:^|[-/])(?:[0-9a-fA-F]{32}"
    r"|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)


def fix_notion_links(text: str) -> tuple[str, list[str]]:
    """Return the body with `p/` restored and the list of original URLs fixed."""
    fixed: list[str] = []

    def repl(m: re.Match[str]) -> str:
        path = m.group(1)
        tail = path.split("?", 1)[0].split("#", 1)[0]
        if not NOTION_PAGE_ID.search(tail):
            return m.group(0)
        fixed.append(m.group(0))
        return "https://app.notion.com/p/" + path

    if "app.notion.com/" not in text:
        return text, fixed
    new_text = NOTION_BAD_LINK.sub(repl, text)
    if fixed:
        log.warning("notion link p/ fixed: %s", fixed)
    return new_text, fixed
