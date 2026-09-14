"""SlackHistoryPort 시험.

`reliability.ports.HistoryReader` Protocol 과 `slack.history.HistoryReader`
실물의 시그니처 차이를 어댑터가 메운다. 확인할 것:
  - oldest float 가 소수 6자리 문자열로 변환돼 실물에 전달된다
  - 판정 불가(HistoryUnavailable)가 예외가 아니라 None 으로 나온다
  - 스레드 조회 실패가 예외를 밖으로 안 내보내고 빈 목록이 된다
  - 스레드 조회 전에 wait_history_slot 이 호출된다
"""

from __future__ import annotations

from typing import Any

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.errors import HistoryUnavailable
from slack_cli_agent.reliability.ports import HistoryReader as HistoryReaderPort
from slack_cli_agent.slack.history import HistoryReader
from slack_cli_agent.slack.history_port import SlackHistoryPort


class FakeWebClient:
    """conversations_history/conversations_replies 를 흉내내는 대역."""

    def __init__(self) -> None:
        self.history_calls: list[dict[str, Any]] = []
        self.replies_calls: list[dict[str, Any]] = []
        self._history_responses: list[dict[str, Any]] = []
        self._replies_response: dict[str, Any] = {"messages": []}
        self._replies_exception: Exception | None = None

    def queue_history(self, response: dict[str, Any]) -> None:
        self._history_responses.append(response)

    def conversations_history(self, **kwargs: Any) -> dict[str, Any]:
        self.history_calls.append(kwargs)
        return self._history_responses.pop(0)

    def set_replies_response(self, response: dict[str, Any]) -> None:
        self._replies_response = response

    def raise_on_replies(self, exc: Exception) -> None:
        self._replies_exception = exc

    def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        self.replies_calls.append(kwargs)
        if self._replies_exception is not None:
            raise self._replies_exception
        return self._replies_response


def _make_port(client: FakeWebClient) -> SlackHistoryPort:
    settings = RuntimeSettings()
    real_reader = HistoryReader(client, settings, clock=lambda: 0.0, sleep=lambda _s: None)
    return SlackHistoryPort(real_reader, client)


def test_isinstance_satisfies_reliability_protocol() -> None:
    client = FakeWebClient()
    port = _make_port(client)
    assert isinstance(port, HistoryReaderPort)


def test_read_history_converts_float_oldest_to_six_decimal_string() -> None:
    client = FakeWebClient()
    client.queue_history({"ok": True, "messages": [{"ts": "1.0"}]})
    port = _make_port(client)

    # 파이썬 str() 이면 소수 7자리가 나올 수 있는 값을 일부러 고른다.
    oldest = 1700000000.1234567

    port.read_history("C123", oldest, 10)

    assert len(client.history_calls) == 1
    sent_oldest = client.history_calls[0]["oldest"]
    assert sent_oldest == "1700000000.123457"
    # 소수점 뒤 정확히 6자리여야 한다 — 7자리면 슬랙이 오류 없이 빈 목록을 준다.
    decimals = sent_oldest.split(".")[1]
    assert len(decimals) == 6


def test_read_history_returns_list_on_success() -> None:
    client = FakeWebClient()
    messages = [{"ts": "1.0", "text": "hello"}]
    client.queue_history({"ok": True, "messages": messages})
    port = _make_port(client)

    result = port.read_history("C123", 1700000000.0, 10)

    assert result == messages


def test_read_history_returns_none_when_unavailable_not_exception() -> None:
    client = FakeWebClient()
    settings = RuntimeSettings()
    # tries 만큼 계속 빈 응답을 채워, 실물이 HistoryUnavailable 을 내게 만든다.
    for _ in range(settings.history_read_tries):
        client.queue_history({"ok": True, "messages": []})
    port = _make_port(client)

    result = port.read_history("C123", 1700000000.0, 10)

    assert result is None


def test_read_history_none_is_distinguishable_from_actual_empty() -> None:
    """실제로 비어 있는 것(빈 목록)과 판정 불가(None)가 같은 값이 되면 안 된다."""
    empty_client = FakeWebClient()
    empty_client.queue_history({"ok": True, "messages": []})
    settings = RuntimeSettings()
    empty_client._history_responses = [{"ok": True, "messages": []}]
    real_reader = HistoryReader(
        empty_client, settings, clock=lambda: 0.0, sleep=lambda _s: None
    )
    # read_history 실물이 첫 시도에 성공하려면 messages 가 비어 있지 않아야 하므로,
    # "실제로 비어 있음"을 확인하려면 실물 대신 대역 reader 로 직접 [] 를 반환시킨다.

    class EmptyResultReader:
        def slack_ts(self, value: Any) -> str:
            return f"{float(value):.6f}"

        def read_history(self, channel: str, oldest: str, limit: int) -> list[dict[str, Any]]:
            return []

    port = SlackHistoryPort(EmptyResultReader(), empty_client)
    result = port.read_history("C123", 1700000000.0, 10)

    assert result == []
    assert result is not None


def test_read_thread_returns_messages_from_conversations_replies() -> None:
    client = FakeWebClient()
    messages = [{"ts": "1.0"}, {"ts": "2.0"}]
    client.set_replies_response({"ok": True, "messages": messages})
    port = _make_port(client)

    result = port.read_thread("C123", "1700000000.000000", 20)

    assert result == messages
    assert client.replies_calls[0]["channel"] == "C123"
    assert client.replies_calls[0]["ts"] == "1700000000.000000"
    assert client.replies_calls[0]["limit"] == 20


def test_read_thread_swallows_any_exception_and_returns_empty_list() -> None:
    client = FakeWebClient()
    client.raise_on_replies(RuntimeError("네트워크 오류"))
    port = _make_port(client)

    result = port.read_thread("C123", "1700000000.000000", 20)

    assert result == []


def test_read_thread_calls_wait_history_slot_before_replies() -> None:
    client = FakeWebClient()
    client.set_replies_response({"ok": True, "messages": []})
    settings = RuntimeSettings()
    real_reader = HistoryReader(client, settings, clock=lambda: 0.0, sleep=lambda _s: None)

    calls: list[str] = []
    original_wait = real_reader.wait_history_slot

    def tracking_wait() -> None:
        calls.append("wait")
        original_wait()

    real_reader.wait_history_slot = tracking_wait  # type: ignore[method-assign]

    port = SlackHistoryPort(real_reader, client)
    port.read_thread("C123", "1700000000.000000", 20)

    assert calls == ["wait"]
    assert len(client.replies_calls) == 1
