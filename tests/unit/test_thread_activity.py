"""SlackThreadActivity — the two reads the watch pre-post duplicate check needs."""

from __future__ import annotations

from typing import Any

from slack_cli_agent.slack.thread_activity import SlackThreadActivity


class FakeIdentity:
    """is_self by user id, the only thing SlackThreadActivity asks of it."""

    def __init__(self, user_id: str) -> None:
        self._user_id = user_id

    def is_self(self, msg: dict[str, Any]) -> bool:
        return msg.get("user") == self._user_id


class FakeTranscript:
    def __init__(self, body: str = "옮긴 대화") -> None:
        self.body = body
        self.calls: list[dict[str, Any]] = []

    def thread_transcript(
        self, channel: str, thread_ts: str, before_ts: Any, after_ts: Any = None,
    ) -> str:
        self.calls.append(
            {"channel": channel, "thread_ts": thread_ts,
             "before_ts": before_ts, "after_ts": after_ts}
        )
        return self.body


class FakeClient:
    def __init__(self, pages: list[dict[str, Any]] | None = None) -> None:
        # each entry is one conversations_replies response
        self.pages = pages or [{"messages": []}]
        self.calls: list[dict[str, Any]] = []
        self.raise_on_call = False

    def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        if self.raise_on_call:
            raise RuntimeError("조회 실패")
        self.calls.append(kwargs)
        return self.pages[min(len(self.calls) - 1, len(self.pages) - 1)]


class FakeSettings:
    history_max_msgs = 100
    history_max_pages = 3


def _activity(client: FakeClient, transcript: FakeTranscript | None = None) -> SlackThreadActivity:
    return SlackThreadActivity(
        client=client,
        identity=FakeIdentity("U_BOT"),
        transcript=transcript or FakeTranscript(),  # type: ignore[arg-type]
        settings=FakeSettings(),  # type: ignore[arg-type]
    )


class TestRepliedSince:
    def test_봇이_since_이후에_올렸으면_참(self) -> None:
        client = FakeClient([{"messages": [
            {"user": "U_HUMAN", "ts": "100.0"},
            {"user": "U_BOT", "ts": "150.0"},
        ]}])
        assert _activity(client).replied_since("C", "90.0", since=120.0) is True

    def test_봇_답이_since_이전이면_거짓(self) -> None:
        client = FakeClient([{"messages": [
            {"user": "U_BOT", "ts": "110.0"},
        ]}])
        assert _activity(client).replied_since("C", "90.0", since=120.0) is False

    def test_since_이후라도_남의_답이면_거짓(self) -> None:
        client = FakeClient([{"messages": [
            {"user": "U_HUMAN", "ts": "200.0"},
            {"user": "U_OTHERBOT", "ts": "210.0"},
        ]}])
        assert _activity(client).replied_since("C", "90.0", since=120.0) is False

    def test_다음_페이지까지_따라가_최신_봇답을_본다(self) -> None:
        client = FakeClient([
            {"messages": [{"user": "U_HUMAN", "ts": "100.0"}],
             "has_more": True, "response_metadata": {"next_cursor": "c2"}},
            {"messages": [{"user": "U_BOT", "ts": "300.0"}]},
        ])
        assert _activity(client).replied_since("C", "90.0", since=120.0) is True
        assert client.calls[1]["cursor"] == "c2"

    def test_조회_실패는_거짓으로_보고를_살린다(self) -> None:
        client = FakeClient()
        client.raise_on_call = True
        assert _activity(client).replied_since("C", "90.0", since=120.0) is False

    def test_ts_불량은_건너뛴다(self) -> None:
        client = FakeClient([{"messages": [{"user": "U_BOT", "ts": "없음"}]}])
        assert _activity(client).replied_since("C", "90.0", since=120.0) is False


class TestTranscriptSince:
    def test_등록_이후만_상한없이_옮긴다(self) -> None:
        tb = FakeTranscript("보고 뒤 대화")
        out = _activity(FakeClient(), tb).transcript_since("C", "90.0", since=95.0)
        assert out == "보고 뒤 대화"
        assert tb.calls[0] == {
            "channel": "C", "thread_ts": "90.0", "before_ts": None, "after_ts": 95.0,
        }
