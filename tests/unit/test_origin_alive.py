"""SlackOriginAliveChecker: True 살아있음 / False 지워짐 / None 모름 (C34)."""

from __future__ import annotations

from typing import Any

from slack_cli_agent.slack.origin_alive import SlackOriginAliveChecker


class FakeSlackError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"error": code}


class FakeClient:
    def __init__(self, *, reply=None, raise_exc: Exception | None = None) -> None:
        self._reply = reply
        self._raise = raise_exc
        self.calls: list[dict[str, Any]] = []

    def conversations_replies(self, **kwargs: Any):
        self.calls.append(kwargs)
        if self._raise is not None:
            raise self._raise
        return self._reply


def test_목록에_ts가_있으면_살아있음_True() -> None:
    client = FakeClient(reply={"messages": [{"ts": "111.222"}]})
    assert SlackOriginAliveChecker(client).alive("C1", "111.222") is True


def test_thread_not_found는_지워짐_False() -> None:
    client = FakeClient(raise_exc=FakeSlackError("thread_not_found"))
    assert SlackOriginAliveChecker(client).alive("C1", "111.222") is False


def test_message_not_found도_지워짐_False() -> None:
    client = FakeClient(raise_exc=FakeSlackError("message_not_found"))
    assert SlackOriginAliveChecker(client).alive("C1", "111.222") is False


def test_그밖의_오류는_모름_None() -> None:
    """조회 실패를 삭제로 단정하지 않는다 — None 은 발송 쪽에서 올린다."""
    client = FakeClient(raise_exc=FakeSlackError("ratelimited"))
    assert SlackOriginAliveChecker(client).alive("C1", "111.222") is None


def test_성공했으나_목록에_없으면_모름_None() -> None:
    """호출은 됐지만 ts 를 못 찾은 경우를 삭제로 단정하지 않는다."""
    client = FakeClient(reply={"messages": [{"ts": "999.000"}]})
    assert SlackOriginAliveChecker(client).alive("C1", "111.222") is None


def test_응답이_비어도_모름_None() -> None:
    client = FakeClient(reply={})
    assert SlackOriginAliveChecker(client).alive("C1", "111.222") is None


def test_확인은_limit을_붙여_conversations_replies로_한다() -> None:
    client = FakeClient(reply={"messages": [{"ts": "111.222"}]})
    SlackOriginAliveChecker(client).alive("C1", "111.222")
    assert client.calls[0]["channel"] == "C1"
    assert client.calls[0]["ts"] == "111.222"
    assert client.calls[0]["limit"] >= 1
