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
    def __init__(self, reader: ChannelHistoryReader, client: Any) -> None:
        self._reader = reader
        self._client = client

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

        Failing to read one thread shouldn't make the whole recovery
        scan inconclusive. wait_history_slot() applies here too since
        thread reads share the same API rate limit.
        """
        self._reader.wait_history_slot()
        try:
            res = self._client.conversations_replies(
                channel=channel, ts=thread_ts, limit=limit
            )
            return res.get("messages") or []
        except Exception:  # noqa: BLE001 - one unreadable thread shouldn't fail the whole scan
            return []
