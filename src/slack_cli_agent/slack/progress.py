"""Shows progress as one Slack message that gets rewritten as steps finish.

Post-then-update rather than the streaming API (chat.startStream /
appendStream): streaming is only granted to apps with the assistant
feature turned on, so a bot without it would get an error on every
request and show nothing at all. chat.update needs only the chat:write
this bot already has for its answers, which makes this the implementation
that works for every bot — the point of keeping it behind ProgressSink.

The placeholder is deleted when the request finishes, so the thread ends
up holding the answer alone. A failed delete leaves the last step line
visible, which is why the lines read as steps ("파일 읽는 중") rather than
as an answer.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from ..core.channel_kind import is_direct_message_channel

log = logging.getLogger(__name__)

#: Slack rejects a message over 4000 characters. Long requests can run
#: through more steps than that, so the oldest lines are dropped rather
#: than the update failing and freezing the display at whatever it last
#: showed.
MAX_LINES = 40


class SlackProgressSink:
    """One request's progress message. Not reusable across requests."""

    def __init__(self, client: Any, channel: str, thread_ts: str, bot_display_name: str = "") -> None:
        self._client = client
        self._channel = channel
        # Same rule as MessagePublisher.post: DMs have no thread to reply in.
        self._thread_ts = None if is_direct_message_channel(channel) else (thread_ts or None)
        self._bot_display_name = bot_display_name
        self._ts: str | None = None
        self._lines: list[str] = []

    def open(self, text: str) -> None:
        kwargs: dict[str, Any] = {"channel": self._channel, "text": text}
        if self._bot_display_name:
            kwargs["username"] = self._bot_display_name
        if self._thread_ts:
            kwargs["thread_ts"] = self._thread_ts
        response = self._client.chat_postMessage(**kwargs)
        self._ts = response["ts"]
        self._lines = [text]

    def append(self, lines: Sequence[str]) -> None:
        # No message to update means open() failed; the session already
        # logged that, and retrying the post here would put the display in
        # the middle of the thread instead of ahead of the answer.
        if self._ts is None:
            return
        self._lines.extend(lines)
        del self._lines[:-MAX_LINES]
        self._client.chat_update(
            channel=self._channel, ts=self._ts, text="\n".join(self._lines),
        )

    def close(self) -> None:
        if self._ts is None:
            return
        ts, self._ts = self._ts, None
        try:
            self._client.chat_delete(channel=self._channel, ts=ts)
        except Exception as exc:  # noqa: BLE001 - a leftover progress line is worse reported than raised
            log.debug("진행 표시 삭제 실패 : %s:%s, %s", self._channel, ts, exc)
