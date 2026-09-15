"""Looks a Slack message up by its timestamp.

conversations.history doesn't return thread replies, and review
reactions almost always land on one, so this uses conversations.replies
— which returns the thread containing the message, and the message
itself when it has no thread. The original bot.py did the same; the
rewrite switched to history and reviews then silently found nothing
(2026-09-15).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)


class SlackMessageLookup:
    """MessageLookupPort implementation."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def find(self, channel: str, ts: str) -> Mapping[str, Any] | None:
        try:
            resp = self._client.conversations_replies(channel=channel, ts=ts, limit=1)
        except Exception as exc:  # noqa: BLE001 - log the failure instead of swallowing it silently
            logger.warning("메시지 조회 실패: channel=%s ts=%s error=%s", channel, ts, exc)
            return None
        messages = (resp or {}).get("messages") or []
        return next((m for m in messages if m.get("ts") == ts), None)
