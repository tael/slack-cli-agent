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
        """Reads one page of a thread; returns an empty list on any failure.

        `limit` caps the whole result. Callers on the request path
        (participants, late addendum, reaction collection) use this and
        stay at one throttled API call per thread. Catch-up, which needs
        the most recent replies, uses read_full_thread instead.
        wait_history_slot() applies here too since thread reads share the
        same API rate limit.
        """
        self._reader.wait_history_slot()
        try:
            res = self._client.conversations_replies(
                channel=channel, ts=thread_ts, limit=limit
            )
            return res.get("messages") or []
        except Exception:  # noqa: BLE001 - callers treat an unreadable thread as empty
            return []

    def read_full_thread(
        self, channel: str, thread_ts: str, page_size: int
    ) -> list[Mapping[str, Any]] | None:
        """Reads a whole thread page by page; None when it can't.

        Replies come oldest-first, so a thread past one page would
        otherwise lose its most recent messages, including the bot's own
        replies, and catch-up would treat an answered mention as missed.
        A failed page, or a thread still going at max_pages, returns None
        rather than a truncated list, so the caller can tell "couldn't
        read" apart from "nothing there". Each page may repeat the parent
        message, so messages are deduplicated by ts. wait_history_slot()
        applies before every page.
        """
        msgs: list[Mapping[str, Any]] = []
        seen: set[str] = set()
        cursor = ""
        try:
            for _ in range(self._max_pages):
                self._reader.wait_history_slot()
                kwargs: dict[str, Any] = {
                    "channel": channel,
                    "ts": thread_ts,
                    "limit": page_size,
                }
                if cursor:
                    kwargs["cursor"] = cursor
                res = self._client.conversations_replies(**kwargs)
                for m in res.get("messages") or []:
                    ts = str(m.get("ts") or "")
                    if ts and ts in seen:
                        continue
                    seen.add(ts)
                    msgs.append(m)
                cursor = (res.get("response_metadata") or {}).get("next_cursor") or ""
                if not res.get("has_more") or not cursor:
                    return msgs
        except Exception:  # noqa: BLE001 - caller maps None to "couldn't tell"
            return None
        return None
