"""Shows progress as one Slack message while the engine runs.

Three implementations, picked at runtime rather than by config:

- SlackTaskCardProgressSink posts a task_card block and rewrites it with
  chat.update. The card carries the current step as its title and the
  finished steps as a bulleted list, and it ends as a complete card that
  stays in the thread. This is what the bot uses.
- SlackStreamingProgressSink uses chat.startStream / appendStream, which
  appends deltas instead of rewriting the whole message. Slack grants
  these only to apps with the agent feature turned on, and only into a
  thread. Not on the runtime path since the task card took the front.
- SlackProgressSink posts once and rewrites with chat.update. It needs
  only the chat:write this bot already has for its answers, so it works
  for every bot, which is why it is the fallback.

FallbackProgressSink runs the first and drops to the second the moment it
raises — a bot whose workspace rejects task_card fails on the very first
call, and that failure must not be the difference between showing progress
and showing nothing.

The fallback's placeholder is deleted when the request finishes, so the
thread ends up holding the answer alone. A failed delete leaves the last
step line visible, which is why the lines read as steps ("파일 읽는 중")
rather than as an answer.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from typing import Any

from ..core.channel_kind import is_direct_message_channel
from ..observability.progress import ProgressSink

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


#: Title the card ends on. While steps are still coming in the title is the
#: step running now, since that is the line a person reads first.
DONE_TITLE = "작업 완료"


def _task_card_details(lines: Sequence[str]) -> dict[str, Any]:
    return {
        "type": "rich_text",
        "elements": [
            {
                "type": "rich_text_list",
                "style": "bullet",
                "elements": [
                    {"type": "rich_text_section", "elements": [{"type": "text", "text": line}]}
                    for line in lines
                ],
            }
        ],
    }


class SlackTaskCardProgressSink:
    """One request's progress card. Not reusable across requests.

    Posts a task_card block and rewrites it with chat.update, same calls as
    SlackProgressSink. The card's title holds the step running now and its
    details hold the steps already done, so the last state stays readable
    without the lines having to read as a sentence.

    Slack takes status only as in_progress / complete / error (confirmed
    2026-09-18 against the real API; completed, failed and cancelled come
    back as invalid_blocks).

    keep_on_close leaves the finished card in the thread above the answer.
    Set it False to get SlackProgressSink's behaviour, where the display is
    deleted and the thread holds the answer alone.
    """

    def __init__(
        self,
        client: Any,
        channel: str,
        thread_ts: str,
        bot_display_name: str = "",
        *,
        keep_on_close: bool = True,
    ) -> None:
        self._client = client
        # Same rule as MessagePublisher.post: DMs have no thread to reply in.
        self._thread_ts = None if is_direct_message_channel(channel) else (thread_ts or None)
        self._channel = channel
        self._bot_display_name = bot_display_name
        self._keep_on_close = keep_on_close
        # Kept for the life of the request: Slack treats an update that
        # changes task_id as a different task.
        self._task_id = str(uuid.uuid4())
        self._ts: str | None = None
        self._lines: list[str] = []

    def open(self, text: str) -> None:
        # FallbackProgressSink replays what was shown so far as one string,
        # so the first text can already hold several lines.
        self._lines = [line for line in text.splitlines() if line.strip()] or [text]
        kwargs: dict[str, Any] = {
            "channel": self._channel,
            "text": self._lines[-1],
            "blocks": [self._card(self._lines[-1], "in_progress")],
        }
        if self._bot_display_name:
            kwargs["username"] = self._bot_display_name
        if self._thread_ts:
            kwargs["thread_ts"] = self._thread_ts
        response = self._client.chat_postMessage(**kwargs)
        self._ts = response["ts"]

    def append(self, lines: Sequence[str]) -> None:
        # No message to update means open() failed; see SlackProgressSink.
        if self._ts is None:
            return
        self._lines.extend(lines)
        del self._lines[:-MAX_LINES]
        self._update(self._lines[-1], "in_progress")

    def close(self) -> None:
        if self._ts is None:
            return
        ts, self._ts = self._ts, None
        try:
            if self._keep_on_close:
                self._update(DONE_TITLE, "complete", ts=ts)
            else:
                self._client.chat_delete(channel=self._channel, ts=ts)
        except Exception as exc:  # noqa: BLE001 - a leftover card is worse reported than raised
            log.debug("진행 카드 정리 실패 : %s:%s, %s", self._channel, ts, exc)

    def _update(self, title: str, status: str, ts: str | None = None) -> None:
        self._client.chat_update(
            channel=self._channel,
            ts=ts or self._ts,
            text=title,
            blocks=[self._card(title, status)],
        )

    def _card(self, title: str, status: str) -> dict[str, Any]:
        return {
            "type": "task_card",
            "task_id": self._task_id,
            "title": title,
            "status": status,
            "details": _task_card_details(self._lines),
        }


class ProgressStreamUnavailable(RuntimeError):
    """Raised when this request has nowhere to stream into.

    Separate from an API failure: it is decided before any call goes out,
    so FallbackProgressSink switches without spending a request.
    """


class SlackStreamingProgressSink:
    """One request's progress stream. Not reusable across requests.

    chat.appendStream sends only the new lines, so there is no MAX_LINES
    equivalent here — the rewrite sink needs one because it resends the
    whole text every tick and Slack rejects a message over 4000
    characters.
    """

    def __init__(
        self,
        client: Any,
        channel: str,
        thread_ts: str,
        *,
        team_id: str,
        user_id: str,
        bot_display_name: str = "",
    ) -> None:
        # chat.startStream takes thread_ts as required, and MessagePublisher.post's
        # rule is that a DM has no thread to reply in. Streaming into one would
        # put the progress display somewhere the answer never goes.
        if is_direct_message_channel(channel) or not thread_ts:
            raise ProgressStreamUnavailable(f"스트리밍할 스레드가 없다 : {channel}")
        # Both recipient ids are required by the API, not optional as the SDK
        # signature suggests — a missing one comes back as
        # missing_recipient_team_id / missing_recipient_user_id (confirmed
        # 2026-09-16 against the real API). Checked here so an identity lookup
        # that hasn't resolved yet costs a switch rather than a rejected request
        # every tick.
        if not team_id or not user_id:
            raise ProgressStreamUnavailable(
                f"스트리밍 수신자를 모른다 : team={team_id!r} user={user_id!r}"
            )
        self._client = client
        self._channel = channel
        self._thread_ts = thread_ts
        self._team_id = team_id
        self._user_id = user_id
        self._bot_display_name = bot_display_name
        self._ts: str | None = None

    def open(self, text: str) -> None:
        kwargs: dict[str, Any] = {
            "channel": self._channel,
            "thread_ts": self._thread_ts,
            "markdown_text": text,
            "recipient_team_id": self._team_id,
            "recipient_user_id": self._user_id,
        }
        if self._bot_display_name:
            kwargs["username"] = self._bot_display_name
        response = self._client.chat_startStream(**kwargs)
        self._ts = response["ts"]

    def append(self, lines: Sequence[str]) -> None:
        # Same reason as the rewrite sink: no ts means open() failed.
        if self._ts is None:
            return
        self._client.chat_appendStream(
            channel=self._channel, ts=self._ts, markdown_text="\n" + "\n".join(lines),
        )

    def close(self) -> None:
        if self._ts is None:
            return
        ts, self._ts = self._ts, None
        # Stop first: deleting a live stream leaves Slack holding an open
        # one, and the delete is what the thread's final shape depends on.
        for call, what in ((self._client.chat_stopStream, "종료"),
                           (self._client.chat_delete, "삭제")):
            try:
                call(channel=self._channel, ts=ts)
            except Exception as exc:  # noqa: BLE001 - a leftover line is worse reported than raised
                log.debug("진행 스트림 %s 실패 : %s:%s, %s", what, self._channel, ts, exc)


class FallbackProgressSink:
    """Streams progress, and switches to the rewrite sink once streaming fails.

    The switch is remembered for the rest of the request: streaming is
    granted per app, so a failure on the first call is a failure on every
    call after, and retrying each tick would cost one rejected request per
    tick. Lines shown so far are replayed into the new sink, so the switch
    doesn't lose what the person was already reading.
    """

    def __init__(
        self,
        primary_factory: Callable[[], ProgressSink],
        fallback_factory: Callable[[], ProgressSink],
    ) -> None:
        self._fallback_factory = fallback_factory
        self._lines: list[str] = []
        self._streaming = True
        try:
            self._sink: ProgressSink = primary_factory()
        except Exception as exc:  # noqa: BLE001 - see ProgressSink
            log.debug("진행 스트리밍을 쓸 수 없다, 고쳐 쓰기로 연다 : %s", exc)
            self._sink = fallback_factory()
            self._streaming = False

    def open(self, text: str) -> None:
        self._lines = [text]
        self._attempt(lambda sink: sink.open(text))

    def append(self, lines: Sequence[str]) -> None:
        self._lines.extend(lines)
        self._attempt(lambda sink: sink.append(list(lines)))

    def close(self) -> None:
        # No fallback at close: there is nothing left to show, and a failed
        # teardown is already tolerated by ProgressSession.
        self._sink.close()

    def _attempt(self, action: Callable[[ProgressSink], None]) -> None:
        try:
            action(self._sink)
            return
        except Exception as exc:
            # 고쳐 쓰기까지 실패한 것은 표시가 아니라 슬랙 쪽 문제다.
            # ProgressSession 이 삼키므로 요청은 그대로 답한다.
            if not self._streaming:
                raise
            log.info("진행 스트리밍 실패, 고쳐 쓰기로 전환 : %s", exc)
        self._switch()

    def _switch(self) -> None:
        self._streaming = False
        failed, self._sink = self._sink, self._fallback_factory()
        try:
            failed.close()
        except Exception as exc:  # noqa: BLE001 - see ProgressSink
            log.debug("스트리밍 표시 정리 실패 : %s", exc)
        self._sink.open("\n".join(self._lines))
