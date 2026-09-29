"""Reads the threads a message links to, so the prompt carries the evidence
instead of relying on the model to go open the link itself.

Each entry carries read_ok separately from the body, because a failed read and
a thread with nothing to transcribe both produce an empty body and the section
tells the model something different about each (sca-678).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .permalinks import parse_slack_links
from .transcript import TranscriptRead

log = logging.getLogger(__name__)


class ThreadTranscript(Protocol):
    def read_thread(
        self, channel: str, thread_ts: str, before_ts: str | float | None
    ) -> TranscriptRead: ...


@dataclass(frozen=True)
class LinkedThread:
    name: str
    body: str
    read_ok: bool


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

    def of(self, text: str, self_channel: str = "") -> tuple[LinkedThread, ...]:
        blocks: list[LinkedThread] = []
        # The cap applies to the links found, not to the ones kept — same as the
        # original, so a self-link doesn't pull a further link into the window.
        for link in parse_slack_links(text)[: self._max_links]:
            # A link back into this very conversation is already attached as past history.
            if self_channel and link.channel == self_channel:
                continue
            read = self._read_of(link.channel, link.thread_ts)
            blocks.append(
                LinkedThread(
                    name=self._name_of(link.channel), body=read.body, read_ok=read.read_ok
                )
            )
        return tuple(blocks)

    def _name_of(self, channel: str) -> str:
        try:
            return self._channel_name(channel) or channel
        except Exception:  # noqa: BLE001 - a name lookup failure falls back to the id
            return channel

    def _read_of(self, channel: str, thread_ts: str) -> TranscriptRead:
        try:
            return self._transcript.read_thread(channel, thread_ts, None)
        except Exception as exc:  # noqa: BLE001 - a failed read must not block the reply
            log.warning("링크된 스레드를 읽지 못했다 : %s %s", channel, exc)
            return TranscriptRead(body="", read_ok=False)
