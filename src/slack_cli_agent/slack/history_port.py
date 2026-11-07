"""Adapter between reliability.ports.HistoryReader (the Protocol) and
slack.history.HistoryReader (the real implementation) — they differ in
how "couldn't tell" is represented (None vs. exception), the type of
`oldest` (float seconds vs. Slack timestamp string), and whether
read_thread exists at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, cast

from slack_cli_agent.core.errors import HistoryUnavailable


class ChannelHistoryReader(Protocol):
    """What this adapter needs. slack.history.HistoryReader satisfies it."""

    def slack_ts(self, value: Any) -> str: ...

    def read_history(self, channel: str, oldest: str, limit: int) -> list[dict[str, Any]]: ...

    def wait_history_slot(self) -> None: ...


class SlackHistoryPort:
    def __init__(
        self, reader: ChannelHistoryReader, client: Any, max_pages: int = 10
    ) -> None:
        self._reader = reader
        self._client = client
        self._max_pages = max(1, max_pages)

    def read_history(
        self, channel: str, oldest: float, limit: int
    ) -> list[Mapping[str, Any]] | None:
        """Converts `oldest` to a Slack timestamp string and calls
        through, translating HistoryUnavailable to None per the
        Protocol's contract — distinct from an actually-empty list.
        """
        oldest_str = self._reader.slack_ts(oldest)
        try:
            result = self._reader.read_history(channel, oldest_str, limit)
        except HistoryUnavailable:
            return None
        # list[dict] isn't a subtype of list[Mapping] — list is
        # invariant in its element type. The elements really are
        # dicts, which do satisfy Mapping, so that's asserted here only.
        return cast("list[Mapping[str, Any]]", result)

    def read_thread(
        self, channel: str, thread_ts: str, limit: int
    ) -> list[Mapping[str, Any]]:
        """Reads an entire thread; returns an empty list on any failure.

        Follows next_cursor page by page (`limit` is the page size).
        Replies come oldest-first, so a thread past one page would
        otherwise lose its most recent messages, including the bot's own
        replies, and catch-up would treat an answered mention as missed.
        For the same reason a failed later page, or a thread still going
        at max_pages, yields an empty list rather than a truncated one.

        Failing to read one thread shouldn't make the whole recovery
        scan inconclusive. wait_history_slot() applies before every page
        since thread reads share the same API rate limit.
        """
        msgs: list[Mapping[str, Any]] = []
        cursor = ""
        try:
            for _ in range(self._max_pages):
                self._reader.wait_history_slot()
                kwargs: dict[str, Any] = {"channel": channel, "ts": thread_ts, "limit": limit}
                if cursor:
                    kwargs["cursor"] = cursor
                res = self._client.conversations_replies(**kwargs)
                msgs.extend(res.get("messages") or [])
                cursor = (res.get("response_metadata") or {}).get("next_cursor") or ""
                if not res.get("has_more") or not cursor:
                    return msgs
        except Exception:  # noqa: BLE001 - one unreadable thread shouldn't fail the whole scan
            return []
        return []
