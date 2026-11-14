"""Strips Slack file permalinks from outgoing bodies.

When a message body carries a Slack file link (`<team>.slack.com/files/...`),
Slack attaches that file to the message again, even with unfurl_links and
unfurl_media off (mametchi measured both on 2026-10-27). A reply that names
the PDF it just uploaded therefore posts the same file card twice in the
thread. Only the link's address is removed; its label stays as plain text.

Message permalinks (`/archives/...`) are not file links and are left alone.

MessagePublisher.post/update call this on every outgoing body, the same
place mametchi's bot.py post() does it (bot.py:6494).
"""

from __future__ import annotations

import re

_FILE_URL = r"https?://[\w.-]+\.slack\.com/files/"
FILE_PERMALINK_MD = re.compile(r"\[([^\]]*)\]\(<?" + _FILE_URL + r"[^\s)>]+>?\)")
FILE_PERMALINK_MRKDWN = re.compile(r"<" + _FILE_URL + r"[^|>\s]+(?:\|([^>]*))?>")
FILE_PERMALINK_BARE = re.compile(_FILE_URL + r"[^\s)>\]]+")


def strip_file_permalinks(text: str) -> str:
    """Return the body with Slack file permalinks removed, keeping link labels."""
    if not text or ".slack.com/files/" not in text:
        return text or ""
    out = FILE_PERMALINK_MD.sub(lambda m: m.group(1), text)
    out = FILE_PERMALINK_MRKDWN.sub(lambda m: m.group(1) or "", out)
    return FILE_PERMALINK_BARE.sub("", out)
