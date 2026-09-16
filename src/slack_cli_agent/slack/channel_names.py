"""Resolves a channel ID to a human-readable name.

The channel config is the first source; only a channel that isn't registered
there goes to Slack. Results are cached, failures included, because this runs
per linked thread and re-asking on every request hits the rate limit.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Protocol

log = logging.getLogger(__name__)


class ChannelInfoClient(Protocol):
    def conversations_info(self, *, channel: str) -> Any: ...


class ChannelNameResolver:
    def __init__(self, client: ChannelInfoClient, configured: Callable[[str], str]) -> None:
        self._client = client
        self._configured = configured
        self._cache: dict[str, str] = {}

    def resolve(self, channel: str) -> str:
        if not channel:
            return ""
        name = self._configured(channel)
        if name:
            return name
        if channel not in self._cache:
            self._cache[channel] = self._lookup(channel)
        return self._cache[channel]

    def _lookup(self, channel: str) -> str:
        try:
            info = self._client.conversations_info(channel=channel)
            return str(info["channel"].get("name") or "")
        except Exception as exc:  # noqa: BLE001 - a failed lookup falls back to the channel id
            log.warning("채널명 조회 실패 %s : %s", channel, exc)
            return ""

    def __call__(self, channel: str) -> str:
        return self.resolve(channel)
