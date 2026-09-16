"""Resolves a channel ID to a human-readable name.

The channel config is the first source; only a channel that isn't registered
there goes to Slack. Results are cached, failures included, because this runs
per linked thread and re-asking on every request hits the rate limit.

Both kinds of cache entry expire, failures sooner: a moment of rate limiting
would otherwise pin a channel to its ID for as long as the process lives, and
a rename would never be picked up (sca-ops).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

log = logging.getLogger(__name__)


class ChannelInfoClient(Protocol):
    def conversations_info(self, *, channel: str) -> Any: ...


class ChannelNameResolver:
    #: 채널 이름이 바뀌는 일은 드물어 자주 다시 볼 이유가 없다.
    DEFAULT_TTL_SEC = 6 * 3600.0
    #: 조회 실패는 대개 잠깐이다. 다음 링크에서 다시 본다.
    DEFAULT_FAILURE_TTL_SEC = 300.0

    def __init__(
        self,
        client: ChannelInfoClient,
        configured: Callable[[str], str],
        *,
        now: Callable[[], float] | None = None,
        ttl_sec: float | None = None,
        failure_ttl_sec: float | None = None,
    ) -> None:
        self._client = client
        self._configured = configured
        self._now = now or time.time
        self._ttl_sec = self.DEFAULT_TTL_SEC if ttl_sec is None else ttl_sec
        self._failure_ttl_sec = (
            self.DEFAULT_FAILURE_TTL_SEC if failure_ttl_sec is None else failure_ttl_sec
        )
        self._cache: dict[str, tuple[str, float]] = {}

    def resolve(self, channel: str) -> str:
        if not channel:
            return ""
        name = self._configured(channel)
        if name:
            return name
        cached = self._cache.get(channel)
        if cached is not None and self._now() - cached[1] < self._ttl_of(cached[0]):
            return cached[0]
        found = self._lookup(channel)
        self._cache[channel] = (found, self._now())
        return found

    def _ttl_of(self, name: str) -> float:
        return self._ttl_sec if name else self._failure_ttl_sec

    def _lookup(self, channel: str) -> str:
        try:
            info = self._client.conversations_info(channel=channel)
            return str(info["channel"].get("name") or "")
        except Exception as exc:  # noqa: BLE001 - a failed lookup falls back to the channel id
            log.warning("채널명 조회 실패 %s : %s", channel, exc)
            return ""

    def __call__(self, channel: str) -> str:
        return self.resolve(channel)
