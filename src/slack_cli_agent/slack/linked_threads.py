"""Reads the threads a message links to, so the prompt carries the evidence
instead of relying on the model to go open the link itself.

Each entry carries read_ok separately from the body, because a failed read and
a thread with nothing to transcribe both produce an empty body and the section
tells the model something different about each (sca-678).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .attachments import unfurl_matches
from .permalinks import SlackLink, parse_slack_links
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
        owner_transcript: ThreadTranscript | None = None,
        owner_channel_name: Callable[[str], str] | None = None,
    ) -> None:
        self._transcript = transcript
        self._channel_name = channel_name
        self._max_links = max_links
        # 소유자 사용자 세션으로 읽는 대화록. 봇이 초대되지 않은 비공개 대화는
        # 봇 토큰으로 channel_not_found 가 나지만 소유자가 멤버면 열린다.
        # 소유자 턴(as_owner)에서만 쓴다. 비소유자 턴에 쓰면 소유자만 볼 수
        # 있는 대화가 그 사람의 답에 실린다(D5, 마메치 linked_thread_context).
        self._owner_transcript = owner_transcript
        self._owner_channel_name = owner_channel_name

    def of(
        self,
        text: str,
        self_channel: str = "",
        attachments: Sequence[Mapping[str, Any]] = (),
        *,
        as_owner: bool = False,
    ) -> tuple[LinkedThread, ...]:
        """`attachments` is the triggering event's own unfurl attachments (not
        the linked thread's), used only when the live read fails — e.g. a
        public channel the bot was never invited to shares a message whose
        link Slack already unfurled for us (gaps.md C39/C40)."""
        blocks: list[LinkedThread] = []
        # The cap applies to the links found, not to the ones kept — same as the
        # original, so a self-link doesn't pull a further link into the window.
        for link in parse_slack_links(text)[: self._max_links]:
            # A link back into this very conversation is already attached as past history.
            if self_channel and link.channel == self_channel:
                continue
            read = self._read_of(self._transcript, link.channel, link.thread_ts)
            name = self._name_of(self._channel_name, link.channel)
            if not read.read_ok and as_owner and self._owner_transcript is not None:
                read = self._read_of(self._owner_transcript, link.channel, link.thread_ts)
                if read.read_ok and name == link.channel and self._owner_channel_name:
                    name = self._name_of(self._owner_channel_name, link.channel)
            body, read_ok = read.body, read.read_ok
            if not read_ok:
                fallback = self._attachment_fallback(link, attachments)
                if fallback:
                    body, read_ok = fallback, True
            blocks.append(LinkedThread(name=name, body=body, read_ok=read_ok))
        return tuple(blocks)

    @staticmethod
    def _attachment_fallback(link: SlackLink, attachments: Sequence[Mapping[str, Any]]) -> str:
        for a in attachments or ():
            if not unfurl_matches(a, link.channel, link.thread_ts):
                continue
            body = a.get("text") or a.get("fallback") or ""
            if body:
                return body
        return ""

    @staticmethod
    def _name_of(resolve: Callable[[str], str], channel: str) -> str:
        try:
            return resolve(channel) or channel
        except Exception:  # noqa: BLE001 - a name lookup failure falls back to the id
            return channel

    @staticmethod
    def _read_of(transcript: ThreadTranscript, channel: str, thread_ts: str) -> TranscriptRead:
        try:
            return transcript.read_thread(channel, thread_ts, None)
        except Exception as exc:  # noqa: BLE001 - a failed read must not block the reply
            log.warning("링크된 스레드를 읽지 못했다 : %s %s", channel, exc)
            return TranscriptRead(body="", read_ok=False)
