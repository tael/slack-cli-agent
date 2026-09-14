"""스레드 참여자 추출.

원본 `thread_people()` 이식. 말한 사람과 멘션으로 불려 들어온 사람을 추린다.
"""

from __future__ import annotations

from typing import Any

import pytest

from slack_cli_agent.slack.participants import ThreadParticipants


class FakeHistory:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages
        self.calls: list[tuple[str, str]] = []

    def read_thread(self, channel: str, thread_ts: str, limit: int) -> list[dict[str, Any]]:
        self.calls.append((channel, thread_ts))
        return self.messages


def names(user_id: str) -> str:
    return {"U1": "말한사람", "U2": "불린사람"}.get(user_id, "")


class TestThreadParticipants:
    def test_말한_사람과_멘션된_사람을_함께_센다(self) -> None:
        history = FakeHistory([{"user": "U1", "text": "<@U2> 봐 주십시오"}])
        people = ThreadParticipants(history, names, bot_user_id="UBOT").of("C1", "1.0")
        assert people == (("말한사람", "<@U1>"), ("불린사람", "<@U2>"))

    def test_같은_사람을_두_번_세지_않는다(self) -> None:
        history = FakeHistory([
            {"user": "U1", "text": "<@U2>"},
            {"user": "U2", "text": "<@U1>"},
        ])
        people = ThreadParticipants(history, names, bot_user_id="UBOT").of("C1", "1.0")
        assert [mention for _, mention in people] == ["<@U1>", "<@U2>"]

    def test_봇_자신은_넣지_않는다(self) -> None:
        history = FakeHistory([{"user": "U1", "text": "<@UBOT> 안녕"}])
        people = ThreadParticipants(history, names, bot_user_id="UBOT").of("C1", "1.0")
        assert people == (("말한사람", "<@U1>"),)

    def test_봇이_보낸_말의_발신자는_넣지_않는다(self) -> None:
        """bot_id 가 붙은 메시지의 user 는 사람이 아니다.

        원본과 같다. 멘션은 그대로 센다 — 봇이 사람을 부른 것도 그 사람이
        알림을 받아 스레드를 보고 있다는 뜻이다.
        """
        history = FakeHistory([{"user": "UOTHERBOT", "bot_id": "B1", "text": "<@U2>"}])
        people = ThreadParticipants(history, names, bot_user_id="UBOT").of("C1", "1.0")
        assert people == (("불린사람", "<@U2>"),)

    def test_이름을_모르면_그렇게_적는다(self) -> None:
        history = FakeHistory([{"user": "U9", "text": ""}])
        people = ThreadParticipants(history, names, bot_user_id="UBOT").of("C1", "1.0")
        assert people == (("이름 모르는 사람", "<@U9>"),)

    def test_디엠_채널은_읽지_않는다(self) -> None:
        """원본 `recent_thread()` 와 같다. 디엠에는 다른 참여자가 없다."""
        history = FakeHistory([{"user": "U1", "text": ""}])
        people = ThreadParticipants(history, names, bot_user_id="UBOT").of("D1", "1.0")
        assert people == ()
        assert history.calls == []
