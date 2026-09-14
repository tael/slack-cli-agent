"""review_ports.py 의 실물 어댑터 4개 테스트.

review/base.py 의 Protocol(MessageLookupPort, TranscriptPort, PermalinkPort,
PublisherPort)을 실제로 만족하는 어댑터가 없어 조립 계층이 점검을 연결하지
못하던 문제를 해결한다. 각 어댑터가 해당 Protocol 의 isinstance 를 통과하는
것과, 슬랙 client·TranscriptBuilder·MessagePublisher 시그니처 차이를 올바르게
흡수하는지를 확인한다.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import pytest

from slack_cli_agent.review.base import (
    MessageLookupPort,
    PermalinkPort,
    PublisherPort,
    TranscriptPort,
)
from slack_cli_agent.slack.review_ports import (
    ReviewPublisher,
    SlackMessageLookup,
    SlackPermalinks,
    ThreadTranscriptPort,
)


class FakeSlackClient:
    """conversations_history/chat_getPermalink 호출 인자를 기록하는 가짜 client."""

    def __init__(
        self,
        history_result: Mapping[str, Any] | None = None,
        history_exc: Exception | None = None,
        permalink_result: Mapping[str, Any] | None = None,
        permalink_exc: Exception | None = None,
    ) -> None:
        self.history_result = history_result or {"messages": []}
        self.history_exc = history_exc
        self.history_calls: list[dict[str, Any]] = []
        self.permalink_result = permalink_result or {}
        self.permalink_exc = permalink_exc
        self.permalink_calls: list[dict[str, Any]] = []

    def conversations_history(self, **kwargs: Any) -> Mapping[str, Any]:
        self.history_calls.append(kwargs)
        if self.history_exc:
            raise self.history_exc
        return self.history_result

    def chat_getPermalink(self, **kwargs: Any) -> Mapping[str, Any]:
        self.permalink_calls.append(kwargs)
        if self.permalink_exc:
            raise self.permalink_exc
        return self.permalink_result


class FakeTranscriptBuilder:
    """TranscriptBuilder 대신 호출 인자만 기록하는 가짜.

    ThreadTranscriptPort 는 진짜 TranscriptBuilder 를 받게 타입을 두었지만,
    시험에서는 호출 인자 기록이 목적이라 duck-typing 으로 대신한다.
    """

    def __init__(self, result: str = "", exc: Exception | None = None) -> None:
        self.result = result
        self.exc = exc
        self.calls: list[tuple[Any, ...]] = []

    def thread_transcript(self, channel: str, thread_ts: str, before_ts: Any) -> str:
        self.calls.append((channel, thread_ts, before_ts))
        if self.exc:
            raise self.exc
        return self.result


class FakePublisher:
    """MessagePublisher 대신 rich 가 키워드로 넘어오는지 검증하는 가짜.

    위치 인자로 넘기면 TypeError 가 나게 rich 를 키워드 전용으로 선언한다.
    """

    def __init__(self, result: str | None = "P1") -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def post(self, channel: str, thread_ts: str, text: str, *, rich: bool) -> str | None:
        self.calls.append({"channel": channel, "thread_ts": thread_ts, "text": text, "rich": rich})
        return self.result


# ---------------------------------------------------------------------------
# SlackMessageLookup
# ---------------------------------------------------------------------------


def test_slack_message_lookup_satisfies_protocol() -> None:
    client = FakeSlackClient()
    assert isinstance(SlackMessageLookup(client), MessageLookupPort)


def test_find_calls_slack_with_correct_args() -> None:
    msg = {"text": "hi"}
    client = FakeSlackClient(history_result={"messages": [msg]})
    lookup = SlackMessageLookup(client)

    result = lookup.find("C1", "111.222")

    assert result == msg
    assert client.history_calls == [
        {"channel": "C1", "latest": "111.222", "oldest": "111.222", "inclusive": True, "limit": 1}
    ]


def test_find_returns_none_when_messages_empty() -> None:
    client = FakeSlackClient(history_result={"messages": []})
    lookup = SlackMessageLookup(client)

    assert lookup.find("C1", "111.222") is None


def test_find_returns_none_when_messages_key_missing() -> None:
    client = FakeSlackClient(history_result={})
    lookup = SlackMessageLookup(client)

    assert lookup.find("C1", "111.222") is None


def test_find_returns_none_and_logs_on_exception(caplog: pytest.LogCaptureFixture) -> None:
    client = FakeSlackClient(history_exc=RuntimeError("network fail"))
    lookup = SlackMessageLookup(client)

    with caplog.at_level(logging.WARNING):
        result = lookup.find("C1", "111.222")

    assert result is None
    assert "network fail" in caplog.text


# ---------------------------------------------------------------------------
# ThreadTranscriptPort
# ---------------------------------------------------------------------------


def test_thread_transcript_port_satisfies_protocol() -> None:
    builder = FakeTranscriptBuilder()
    assert isinstance(ThreadTranscriptPort(builder), TranscriptPort)


def test_transcript_delegates_with_before_ts_none() -> None:
    builder = FakeTranscriptBuilder(result="대화록 본문")
    port = ThreadTranscriptPort(builder)

    result = port.transcript("C1", "222.333")

    assert result == "대화록 본문"
    assert builder.calls == [("C1", "222.333", None)]


def test_transcript_returns_empty_and_logs_on_exception(caplog: pytest.LogCaptureFixture) -> None:
    builder = FakeTranscriptBuilder(exc=RuntimeError("boom"))
    port = ThreadTranscriptPort(builder)

    with caplog.at_level(logging.WARNING):
        result = port.transcript("C1", "222.333")

    assert result == ""
    assert "boom" in caplog.text


# ---------------------------------------------------------------------------
# SlackPermalinks
# ---------------------------------------------------------------------------


def test_slack_permalinks_satisfies_protocol() -> None:
    client = FakeSlackClient()
    assert isinstance(SlackPermalinks(client), PermalinkPort)


def test_permalink_returns_value_from_response() -> None:
    client = FakeSlackClient(permalink_result={"permalink": "https://slack.example/x"})
    port = SlackPermalinks(client)

    result = port.permalink("C1", "333.444")

    assert result == "https://slack.example/x"
    assert client.permalink_calls == [{"channel": "C1", "message_ts": "333.444"}]


def test_permalink_returns_empty_string_on_exception() -> None:
    client = FakeSlackClient(permalink_exc=RuntimeError("no perm"))
    port = SlackPermalinks(client)

    assert port.permalink("C1", "333.444") == ""


def test_permalink_returns_empty_string_when_missing() -> None:
    client = FakeSlackClient(permalink_result={})
    port = SlackPermalinks(client)

    assert port.permalink("C1", "333.444") == ""


# ---------------------------------------------------------------------------
# ReviewPublisher
# ---------------------------------------------------------------------------


def test_review_publisher_satisfies_protocol() -> None:
    publisher = FakePublisher()
    assert isinstance(ReviewPublisher(publisher), PublisherPort)


def test_post_converts_none_thread_ts_to_empty_string() -> None:
    publisher = FakePublisher()
    port = ReviewPublisher(publisher)

    port.post("C1", None, "본문", rich=True)

    assert publisher.calls == [{"channel": "C1", "thread_ts": "", "text": "본문", "rich": True}]


def test_post_passes_rich_as_keyword() -> None:
    publisher = FakePublisher()
    port = ReviewPublisher(publisher)

    # FakePublisher.post 가 rich 를 키워드 전용으로 선언했으므로, ReviewPublisher
    # 가 위치 인자로 넘기면 여기서 TypeError 가 난다.
    port.post("C1", "999.000", "본문", rich=False)

    assert publisher.calls[0]["rich"] is False


def test_post_returns_publisher_result_as_is() -> None:
    publisher = FakePublisher(result=None)
    port = ReviewPublisher(publisher)

    assert port.post("C1", "999.000", "본문", rich=True) is None

    publisher2 = FakePublisher(result="P42")
    port2 = ReviewPublisher(publisher2)
    assert port2.post("C1", "999.000", "본문", rich=True) == "P42"
