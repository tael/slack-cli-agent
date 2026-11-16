"""Earlier messages of a colleague's DM, handed to the next DM turn.

Each DM turn runs alone with no session (service.py), so the model saw only
the newest line. On 2026-11-16 a colleague asked "그걸 어떻게 보는데?" and
"뭔소리야? 이해가 안돼" right after the bot's own answers, and the bot
replied that it could not tell what "그걸" meant. This reads the DM's recent
history from Slack and renders it as a <dm_history> block, so the turn can
resolve references to what was just said.

A DM channel has two parties only: messages from `user` are the colleague's,
everything else is the bot's. Any failure returns "" and the turn runs as
before; a missing history must not cost the reply.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

#: How far back to look. A DM conversation older than this is a new topic.
WINDOW_SEC = 6 * 3600
#: Messages fetched from Slack, newest first.
FETCH_LIMIT = 30
#: Earlier messages kept in the block, oldest first.
KEEP_MESSAGES = 16
#: Longest text kept per message, and for the whole block.
MAX_MESSAGE_CHARS = 1200
MAX_BLOCK_CHARS = 8000


def _drop_current(messages: list[dict[str, Any]], user: str, text: str) -> list[dict[str, Any]]:
    """Removes the DM being answered: by the time the turn runs it is
    already in the history. Takes the newest colleague message with the
    same text; if none matches, the newest colleague message."""
    target = (text or "").strip()
    for i, msg in enumerate(messages):
        if msg.get("user") == user and (msg.get("text") or "").strip() == target:
            return messages[:i] + messages[i + 1:]
    for i, msg in enumerate(messages):
        if msg.get("user") == user:
            return messages[:i] + messages[i + 1:]
    return messages


def render(messages: list[dict[str, Any]], user: str, text: str) -> str:
    """`messages` newest first, as conversations_history returns them."""
    rows = [
        m for m in messages
        if (m.get("text") or "").strip() and m.get("subtype") not in ("channel_join", "message_deleted")
    ]
    rows = _drop_current(rows, user, text)[:KEEP_MESSAGES]
    lines: list[str] = []
    for msg in reversed(rows):
        who = "동료" if msg.get("user") == user else "봇"
        body = (msg.get("text") or "").strip()
        if len(body) > MAX_MESSAGE_CHARS:
            body = body[:MAX_MESSAGE_CHARS] + " …(생략)"
        lines.append(f"[{who}] {body}")
    if not lines:
        return ""
    joined = "\n".join(lines)
    if len(joined) > MAX_BLOCK_CHARS:
        joined = "…(앞부분 생략)\n" + joined[-MAX_BLOCK_CHARS:]
    return "<dm_history>\n" + joined + "\n</dm_history>\n"


class DmHistory:
    def __init__(self, client: Any, clock: Callable[[], float] = time.time) -> None:
        self._client = client
        self._clock = clock

    def __call__(self, channel: str, user: str, text: str) -> str:
        try:
            res = self._client.conversations_history(
                channel=channel,
                oldest=f"{self._clock() - WINDOW_SEC:.6f}",
                limit=FETCH_LIMIT,
            )
            messages = list(res.get("messages") or [])
        except Exception as exc:  # noqa: BLE001 - the reply goes out without history
            log.warning("DM 기록을 읽지 못했다 : %s : %s", channel, exc)
            return ""
        return render(messages, user, text)
