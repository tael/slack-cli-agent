"""Real Slack adapters satisfying the Protocols in review/base.py.

ReviewTask only takes MessageLookupPort/TranscriptPort/PermalinkPort/
PublisherPort as Protocols; no concrete adapter existed, so wiring
couldn't connect postmortems, debug traces, or format review to
anything. These four classes are that adapter — all constructor-
injected, none creates its own Slack client.

Each adapter also absorbs the places where
TranscriptBuilder.thread_transcript and MessagePublisher.post differ
from the Protocol signature here (argument count, whether None is
accepted).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from slack_cli_agent.config.channel import ChannelConfig
from slack_cli_agent.observability.progress import ProgressCoordinator
from slack_cli_agent.review.base import ReviewTarget
from slack_cli_agent.slack.message_lookup import SlackMessageLookup
from slack_cli_agent.slack.publisher import MessagePublisher
from slack_cli_agent.slack.transcript import TranscriptBuilder

logger = logging.getLogger(__name__)

__all__ = [
    "ReviewProgressDisplay",
    "ReviewPublisher",
    "SlackMessageLookup",
    "SlackPermalinks",
    "ThreadTranscriptPort",
]


class ChannelLookup(Protocol):
    def get(self, channel: str) -> ChannelConfig | None: ...


class ReviewProgressDisplay:
    """ReviewProgressPort implementation, reusing the request pipeline's coordinator.

    The display goes in the thread the reaction was added to, not the
    troubleshoot channel the report lands in: that thread is where the
    person who asked for the review is looking. Whether it appears at all
    is the source channel's own `progress` setting, same as a chat request.
    """

    def __init__(self, coordinator: ProgressCoordinator, channels: ChannelLookup) -> None:
        self._coordinator = coordinator
        self._channels = channels

    @contextmanager
    def display(self, target: ReviewTarget, thread_ts: str) -> Iterator[Path | None]:
        config = self._channels.get(target.channel)
        log_path = self._coordinator.log_path_for(config, target.channel, target.ts)
        # by_user is the reactor, which is who Slack's streaming API needs as
        # the recipient -- the review was asked for by them, not by the author
        # of the message being reviewed.
        with self._coordinator.session(target.channel, thread_ts, target.by_user, log_path):
            yield log_path


class ThreadTranscriptPort:
    """TranscriptPort implementation, absorbing TranscriptBuilder's signature differences."""

    def __init__(self, builder: TranscriptBuilder) -> None:
        self._builder = builder

    def transcript(self, channel: str, thread_ts: str) -> str:
        try:
            # Reviews look at the whole thread, so before_ts is always None.
            return self._builder.thread_transcript(channel, thread_ts, None)
        except Exception as exc:  # noqa: BLE001 - an unreadable transcript shouldn't stop the review
            logger.warning(
                "대화록 조회 실패: channel=%s thread_ts=%s error=%s", channel, thread_ts, exc
            )
            return ""


class SlackPermalinks:
    """PermalinkPort implementation."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def permalink(self, channel: str, ts: str) -> str:
        try:
            resp = self._client.chat_getPermalink(channel=channel, message_ts=ts)
        except Exception as exc:  # noqa: BLE001 - the link is optional; the review still produces a result without it
            logger.warning("영구 링크 조회 실패: channel=%s ts=%s error=%s", channel, ts, exc)
            return ""

        return (resp or {}).get("permalink") or ""


class ReviewPublisher:
    """PublisherPort implementation, absorbing MessagePublisher's optional thread_ts."""

    def __init__(self, publisher: MessagePublisher) -> None:
        self._publisher = publisher

    def post(self, channel: str, thread_ts: str | None, text: str, *, rich: bool) -> str | None:
        return self._publisher.post(channel, thread_ts or "", text, rich=rich)
