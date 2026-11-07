"""Fixes outgoing links before they reach Slack.

`NotionLinkGuard` restores the missing `p/` in app.notion.com page links.
Notion's canonical page URL path starts with `p/`; without it the link is a
404, with or without a workspace name in front of the id. The SQL mode of
notion-query-data-sources returns its url column in that broken shape, so
copying a query result verbatim posts a dead link. Readers only find out by
clicking, and a posted link can't be taken back, so it is fixed here.

Only links whose path ends in a page id are touched. Adding `p/` to other
paths (login, install) would break those instead.
"""

from __future__ import annotations

import logging
import re
from typing import ClassVar

from slack_cli_agent.guard.base import GuardContext, GuardResult, OutputGuard

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

    return NOTION_BAD_LINK.sub(repl, text), fixed


class NotionLinkGuard(OutputGuard):
    name: ClassVar[str] = "notion_link"

    def apply(self, body: str, ctx: GuardContext) -> GuardResult:
        if "app.notion.com/" not in body:
            return GuardResult(body=body, changed=False)
        new_body, fixed = fix_notion_links(body)
        if not fixed:
            return GuardResult(body=body, changed=False)
        log.warning("notion link p/ fixed: %s", fixed)
        return GuardResult(body=new_body, changed=True, detail={"fixed": fixed})
