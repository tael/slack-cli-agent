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
from pathlib import Path
from typing import Any

import pytest

from slack_cli_agent.config.channel import ChannelConfig
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.observability.progress import ProgressCoordinator
from slack_cli_agent.review.base import (
    MessageLookupPort,
    PermalinkPort,
    PublisherPort,
    ReviewProgressPort,
    ReviewTarget,
    TranscriptPort,
)
from slack_cli_agent.slack.review_ports import (
    ReviewProgressDisplay,
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
        self.replies_result: Mapping[str, Any] = {"messages": []}
        self.replies_exc: Exception | None = None
        self.replies_calls: list[dict[str, Any]] = []

    def conversations_history(self, **kwargs: Any) -> Mapping[str, Any]:
        self.history_calls.append(kwargs)
        if self.history_exc:
            raise self.history_exc
        return self.history_result

    def conversations_replies(self, **kwargs: Any) -> Mapping[str, Any]:
        self.replies_calls.append(kwargs)
        if self.replies_exc:
            raise self.replies_exc
        return self.replies_result

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


# SlackMessageLookup


def test_slack_message_lookup_satisfies_protocol() -> None:
    client = FakeSlackClient()
    assert isinstance(SlackMessageLookup(client), MessageLookupPort)


def test_find_calls_slack_with_correct_args() -> None:
    msg = {"ts": "111.222", "text": "hi"}
    client = FakeSlackClient()
    client.replies_result = {"messages": [msg]}
    lookup = SlackMessageLookup(client)

    result = lookup.find("C1", "111.222")

    assert result == msg
    assert client.replies_calls == [{"channel": "C1", "ts": "111.222", "limit": 1}]


def test_find_uses_replies_so_thread_replies_are_found() -> None:
    """conversations.history 는 스레드 답글을 안 돌려준다. 점검 이모지는 대개 답글에 달린다."""
    client = FakeSlackClient()
    client.replies_result = {"messages": [{"ts": "1.0"}, {"ts": "111.222", "text": "답글"}]}
    lookup = SlackMessageLookup(client)

    assert lookup.find("C1", "111.222") == {"ts": "111.222", "text": "답글"}
    assert client.history_calls == []


def test_find_returns_none_when_messages_empty() -> None:
    client = FakeSlackClient()
    lookup = SlackMessageLookup(client)

    assert lookup.find("C1", "111.222") is None


def test_find_returns_none_when_messages_key_missing() -> None:
    client = FakeSlackClient()
    client.replies_result = {}
    lookup = SlackMessageLookup(client)

    assert lookup.find("C1", "111.222") is None


def test_find_returns_none_and_logs_on_exception(caplog: pytest.LogCaptureFixture) -> None:
    client = FakeSlackClient()
    client.replies_exc = RuntimeError("network fail")
    lookup = SlackMessageLookup(client)

    with caplog.at_level(logging.WARNING):
        result = lookup.find("C1", "111.222")

    assert result is None
    assert "network fail" in caplog.text


# ThreadTranscriptPort


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


# SlackPermalinks


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


# ReviewPublisher


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


class Test진행_표시_어댑터:
    """점검이 도는 동안 원 스레드에 진행 표시를 띄운다(sca-tfd).

    표시 자리는 결과가 올라가는 트러블슈팅 채널이 아니라 사용자가 리액션을
    붙인 원 스레드다 - 사용자가 보고 있는 곳이 거기다.
    """

    def _coordinator(self, tmp_path: Path, sink: Any) -> ProgressCoordinator:
        return ProgressCoordinator(
            settings=RuntimeSettings(),
            sink_factory=lambda channel, thread_ts, user: sink,
            log_dir=tmp_path,
        )

    def test_Protocol_을_만족한다(self, tmp_path: Path) -> None:
        표시 = ReviewProgressDisplay(
            self._coordinator(tmp_path, _수집싱크()), _채널들(progress=True)
        )
        assert isinstance(표시, ReviewProgressPort)

    def test_진행이_켜진_채널이면_로그_경로를_내고_표시를_연다(self, tmp_path: Path) -> None:
        싱크 = _수집싱크()
        표시 = ReviewProgressDisplay(self._coordinator(tmp_path, 싱크), _채널들(progress=True))
        대상 = ReviewTarget(channel="C1", ts="1.1", by_user="U2", channel_name="채널", rich=True)
        with 표시.display(대상, "100.0") as 경로:
            assert 경로 is not None
            assert 경로.parent == tmp_path
        assert 싱크.opened == 1
        assert 싱크.closed == 1

    def test_진행이_꺼진_채널이면_아무것도_안_띄운다(self, tmp_path: Path) -> None:
        싱크 = _수집싱크()
        표시 = ReviewProgressDisplay(self._coordinator(tmp_path, 싱크), _채널들(progress=False))
        대상 = ReviewTarget(channel="C1", ts="1.1", by_user="U2", channel_name="채널", rich=True)
        with 표시.display(대상, "100.0") as 경로:
            assert 경로 is None
        assert 싱크.opened == 0

    def test_표시는_원_스레드에_붙는다(self, tmp_path: Path) -> None:
        받은인자: list[tuple[str, str, str]] = []

        def 기록하는_싱크(channel: str, thread_ts: str, user: str) -> _수집싱크:
            받은인자.append((channel, thread_ts, user))
            return _수집싱크()

        coordinator = ProgressCoordinator(
            settings=RuntimeSettings(),
            sink_factory=기록하는_싱크,
            log_dir=tmp_path,
        )
        표시 = ReviewProgressDisplay(coordinator, _채널들(progress=True))
        대상 = ReviewTarget(channel="C1", ts="1.1", by_user="U2", channel_name="채널", rich=True)
        with 표시.display(대상, "100.0"):
            pass
        assert 받은인자 == [("C1", "100.0", "U2")]


class _수집싱크:
    def __init__(self) -> None:
        self.opened = 0
        self.closed = 0
        self.lines: list[str] = []

    def open(self, text: str) -> None:
        self.opened += 1

    def append(self, lines: Any) -> None:
        self.lines.extend(lines)

    def close(self) -> None:
        self.closed += 1


class _채널들:
    def __init__(self, *, progress: bool) -> None:
        self._config = ChannelConfig(channel_id="C1", name="채널", progress=progress)

    def get(self, channel: str) -> ChannelConfig | None:
        return self._config
