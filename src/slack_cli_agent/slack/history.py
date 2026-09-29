"""Slack's conversations_history can return ok=True with an empty
`messages` list. On 2026-09-11, 956 of 3000 sampled timestamps (about
31%) had 7 decimal digits; sending one of those as `oldest` makes
Slack silently return an empty list, indistinguishable from a channel
that's genuinely empty. Retrying tells the two apart.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.errors import HistoryUnavailable


class HistoryReader:
    def __init__(
        self,
        client: Any,
        settings: RuntimeSettings,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._settings = settings
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_call = 0.0

    def slack_ts(self, value: Any) -> str:
        """Formats a time.time() value as Slack's 6-decimal timestamp.

        str(time.time()) can round-trip to 7 decimal digits; sending
        that as oldest gets back an empty list with ok=True, which
        reads as "no messages" instead of "malformed timestamp".
        """
        return f"{float(value):.6f}"

    def wait_history_slot(self) -> None:
        """Throttles history calls — better to avoid the empty response than retry after it."""
        with self._lock:
            gap = self._clock() - self._last_call
            min_interval = self._settings.history_min_interval_sec
            if gap < min_interval:
                self._sleep(min_interval - gap)
            self._last_call = self._clock()

    def read_history(self, channel: str, oldest: str, limit: int) -> list[dict[str, Any]]:
        """Reads channel history, retrying rather than trusting an
        empty result.

        Raises HistoryUnavailable instead of returning None on
        repeated empty reads, so an actually-empty history and
        "couldn't tell" are distinguishable by type.
        """
        empty = 0
        tries = self._settings.history_read_tries
        for attempt in range(tries):
            self.wait_history_slot()
            res = self._client.conversations_history(
                channel=channel, oldest=oldest, limit=limit
            )
            msgs = res.get("messages") or []
            if msgs:
                return msgs
            empty += 1
            if attempt < tries - 1:
                self._sleep(self._settings.history_read_pause_sec)
        raise HistoryUnavailable(
            f"{channel} 기록이 {empty}번 내리 비어 있다. 없다고 단정하지 않는다."
        )
