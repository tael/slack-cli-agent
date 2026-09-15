"""Reads the threads a message links to, so the prompt carries the evidence
instead of relying on the model to go open the link itself.

An empty body means the read failed — the section renders that as an explicit
"couldn't read it", which is different from the thread being empty.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

from .permalinks import parse_slack_links

log = logging.getLogger(__name__)


class ThreadTranscript(Protocol):
    def thread_transcript(
        self, channel: str, thread_ts: str, before_ts: str | float | None
    ) -> str: ...


class LinkedThreadReader:
    def __init__(
        self,
        transcript: ThreadTranscript,
        channel_name: Callable[[str], str],
        max_links: int = 3,
    ) -> None:
        self._transcript = transcript
        self._channel_name = channel_name
        self._max_links = max_links

    def of(self, text: str, self_channel: str = "") -> tuple[tuple[str, str], ...]:
        """(channel name, transcript) pairs; an empty transcript means the read failed."""
        blocks: list[tuple[str, str]] = []
        # The cap applies to the links found, not to the ones kept — same as the
        # original, so a self-link doesn't pull a further link into the window.
        for link in parse_slack_links(text)[: self._max_links]:
            # A link back into this very conversation is already attached as past history.
            if self_channel and link.channel == self_channel:
                continue
            blocks.append((self._name_of(link.channel), self._body_of(link.channel, link.thread_ts)))
        return tuple(blocks)

    def _name_of(self, channel: str) -> str:
        try:
            return self._channel_name(channel) or channel
        except Exception:  # noqa: BLE001 - a name lookup failure falls back to the id
            return channel

    def _body_of(self, channel: str, thread_ts: str) -> str:
        try:
            return self._transcript.thread_transcript(channel, thread_ts, None)
        except Exception as exc:  # noqa: BLE001 - a failed read must not block the reply
            log.warning("링크된 스레드를 읽지 못했다 : %s %s", channel, exc)
            return ""
