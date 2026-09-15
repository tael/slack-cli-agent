"""Looks a Slack message up by its timestamp.

conversations.history doesn't return thread replies, and both callers —
the review reaction check and ReviewTask — mostly deal with replies, so
an empty history result falls through to conversations.replies. Both
callers share this class; when the lookup lived only in review_ports,
the listener grew its own copy and only one of the two got the fallback.
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
        message = self._first(self._from_history, channel, ts)
        if message is not None:
            return message
        return self._matching(self._from_replies, channel, ts)

    def _first(self, finder: Any, channel: str, ts: str) -> Mapping[str, Any] | None:
        messages = self._call(finder, channel, ts)
        return messages[0] if messages else None

    def _matching(self, finder: Any, channel: str, ts: str) -> Mapping[str, Any] | None:
        for message in self._call(finder, channel, ts):
            if message.get("ts") == ts:
                return message
        return None

    def _call(self, finder: Any, channel: str, ts: str) -> list[Mapping[str, Any]]:
        try:
            resp = finder(channel, ts)
        except Exception as exc:  # noqa: BLE001 - log the failure instead of swallowing it silently
            logger.warning("메시지 조회 실패: channel=%s ts=%s error=%s", channel, ts, exc)
            return []
        return list((resp or {}).get("messages") or [])

    def _from_history(self, channel: str, ts: str) -> Mapping[str, Any]:
        return self._client.conversations_history(  # type: ignore[no-any-return]
            channel=channel, latest=ts, oldest=ts, inclusive=True, limit=1
        )

    def _from_replies(self, channel: str, ts: str) -> Mapping[str, Any]:
        return self._client.conversations_replies(channel=channel, ts=ts, limit=200)  # type: ignore[no-any-return]
