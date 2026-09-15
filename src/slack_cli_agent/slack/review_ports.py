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
from typing import Any

from slack_cli_agent.slack.message_lookup import SlackMessageLookup
from slack_cli_agent.slack.publisher import MessagePublisher
from slack_cli_agent.slack.transcript import TranscriptBuilder

logger = logging.getLogger(__name__)

__all__ = ["ReviewPublisher", "SlackMessageLookup", "SlackPermalinks", "ThreadTranscriptPort"]


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
