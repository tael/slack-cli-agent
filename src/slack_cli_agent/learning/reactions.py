"""Collects human reactions left on threads under bot responses.

A message counts as a reaction only if a bot message precedes it in the
thread — the assumption is that anything a human says after the bot replied
is feedback on that reply. A thread lookup failure is skipped rather than
aborting the whole collection.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from slack_cli_agent.learning.ports import ResponseArchiveReader
from slack_cli_agent.reliability.ports import HistoryReader


class ReactionCollector:
    """Extracts thread timestamps from response history and collects reactions."""

    def __init__(
        self,
        archive: ResponseArchiveReader,
        *,
        threads: HistoryReader,
        thread_timestamps: Callable[[str], tuple[str, ...]],
        channel_id_of: Callable[[str], str | None],
        limit: int,
    ) -> None:
        self._archive = archive
        self._threads = threads
        self._thread_timestamps = thread_timestamps
        self._channel_id_of = channel_id_of
        self._limit = limit

    def collect(self, day: str) -> Mapping[str, tuple[Mapping[str, object], ...]]:
        result: dict[str, tuple[Mapping[str, object], ...]] = {}
        for channel_name, text in self._archive.read_day(day).items():
            channel_id = self._channel_id_of(channel_name)
            if channel_id is None:
                continue
            reactions = self._reactions_in_channel(channel_id, channel_name, text)
            if reactions:
                result[channel_name] = tuple(reactions)
        return result

    def _reactions_in_channel(
        self, channel_id: str, channel_name: str, text: str
    ) -> list[Mapping[str, object]]:
        reactions: list[Mapping[str, object]] = []
        for ts in self._thread_timestamps(text):
            try:
                messages = self._threads.read_thread(channel_id, ts, self._limit)
            except Exception:  # noqa: BLE001, S112 — one thread failing shouldn't stop the whole batch
                continue
            reactions.extend(self._reactions_in_thread(channel_name, ts, messages))
        return reactions

    def _reactions_in_thread(
        self, channel_name: str, ts: str, messages: list[Mapping[str, Any]]
    ) -> list[Mapping[str, object]]:
        out: list[Mapping[str, object]] = []
        for i, message in enumerate(messages):
            if message.get("bot_id"):
                continue
            prev_bot = any(m.get("bot_id") for m in messages[:i])
            if prev_bot and (message.get("text") or "").strip():
                out.append(
                    {
                        "channel": channel_name,
                        "thread": ts,
                        "text": str(message["text"])[:1000],
                    }
                )
        return out
