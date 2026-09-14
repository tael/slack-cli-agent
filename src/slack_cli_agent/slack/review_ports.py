"""review/base.py 의 Protocol 을 만족하는 실물 슬랙 어댑터.

`ReviewTask` 는 `MessageLookupPort`/`TranscriptPort`/`PermalinkPort`/`PublisherPort`
를 Protocol 로만 받는다. 그런데 이 계약을 그대로 만족하는 실물 어댑터가 없어서
조립 계층이 점검(부검·디버그 추적·서식 점검)을 연결하지 못했다. 이 파일이 그
어댑터 네 개다. 전부 생성자 주입이고 슬랙 client 를 직접 만들지 않는다.

각 어댑터는 `TranscriptBuilder.thread_transcript`, `MessagePublisher.post` 의
시그니처가 `review/base.py` 의 Protocol 과 다른 지점(인자 개수, `None` 허용
여부)을 흡수하는 역할도 겸한다.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from slack_cli_agent.slack.publisher import MessagePublisher
from slack_cli_agent.slack.transcript import TranscriptBuilder

logger = logging.getLogger(__name__)


class SlackMessageLookup:
    """MessageLookupPort 구현. 지목한 메시지 원문 하나를 조회한다."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def find(self, channel: str, ts: str) -> Mapping[str, Any] | None:
        try:
            resp = self._client.conversations_history(
                channel=channel, latest=ts, oldest=ts, inclusive=True, limit=1
            )
        except Exception as exc:  # noqa: BLE001 — 조회 실패를 삼키지 않고 기록한다
            logger.warning("메시지 조회 실패: channel=%s ts=%s error=%s", channel, ts, exc)
            return None

        messages = (resp or {}).get("messages") or []
        return messages[0] if messages else None


class ThreadTranscriptPort:
    """TranscriptPort 구현. TranscriptBuilder 의 시그니처 차이를 흡수한다."""

    def __init__(self, builder: TranscriptBuilder) -> None:
        self._builder = builder

    def transcript(self, channel: str, thread_ts: str) -> str:
        try:
            # 점검은 스레드 전부를 본다 — before_ts 는 항상 None 이다.
            return self._builder.thread_transcript(channel, thread_ts, None)
        except Exception as exc:  # noqa: BLE001 — 대화록을 못 읽었다고 점검 전체를 멈추지 않는다
            logger.warning(
                "대화록 조회 실패: channel=%s thread_ts=%s error=%s", channel, thread_ts, exc
            )
            return ""


class SlackPermalinks:
    """PermalinkPort 구현."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def permalink(self, channel: str, ts: str) -> str:
        try:
            resp = self._client.chat_getPermalink(channel=channel, message_ts=ts)
        except Exception as exc:  # noqa: BLE001 — 링크는 부가 정보다. 없어도 점검 결과는 낸다
            logger.warning("영구 링크 조회 실패: channel=%s ts=%s error=%s", channel, ts, exc)
            return ""

        return (resp or {}).get("permalink") or ""


class ReviewPublisher:
    """PublisherPort 구현. MessagePublisher 의 thread_ts 가 None 일 수 있는 차이를 흡수한다."""

    def __init__(self, publisher: MessagePublisher) -> None:
        self._publisher = publisher

    def post(self, channel: str, thread_ts: str | None, text: str, *, rich: bool) -> str | None:
        return self._publisher.post(channel, thread_ts or "", text, rich=rich)
