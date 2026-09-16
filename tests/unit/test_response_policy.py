"""ResponsePolicy: 이름을 안 부른 스레드 답글에 나설지 판정한다."""

from __future__ import annotations

import pytest

from slack_cli_agent.config.channel import ChannelConfig
from slack_cli_agent.slack.gate import ResponseGate
from slack_cli_agent.slack.policy import ResponsePolicy, ThreadState


@pytest.fixture
def policy() -> ResponsePolicy:
    return ResponsePolicy(ResponseGate())


def 채널(**overrides: object) -> ChannelConfig:
    return ChannelConfig.from_dict("C1", dict(overrides))


class TestConsiders:
    def test_등록안된_채널은_대상이_아니다(self, policy: ResponsePolicy) -> None:
        assert policy.considers(None) is False

    def test_answer_unaddressed가_꺼져_있으면_대상이_아니다(
        self, policy: ResponsePolicy
    ) -> None:
        assert policy.considers(채널()) is False

    def test_켜져_있으면_대상이다(self, policy: ResponsePolicy) -> None:
        assert policy.considers(채널(answer_unaddressed=True)) is True


class TestAnswers:
    def test_봇이_안_낀_스레드는_안_받는다(self, policy: ResponsePolicy) -> None:
        state = ThreadState(joined=False, bot_asked=False)
        assert policy.answers(채널(answer_unaddressed=True), "이거 해줘", state) is False

    def test_봇이_낀_스레드의_용건은_받는다(self, policy: ResponsePolicy) -> None:
        state = ThreadState(joined=True, bot_asked=False)
        assert policy.answers(채널(answer_unaddressed=True), "이거 해줘", state) is True

    def test_감탄사만_있는_말은_안_받는다(self, policy: ResponsePolicy) -> None:
        state = ThreadState(joined=True, bot_asked=False)
        assert policy.answers(채널(answer_unaddressed=True), "감사합니다", state) is False

    def test_봇이_되물은_뒤의_짧은_답은_받는다(self, policy: ResponsePolicy) -> None:
        """되물음에 대한 'ㅇㅇ' 는 감탄사가 아니라 승인이다."""
        state = ThreadState(joined=True, bot_asked=True)
        assert policy.answers(채널(answer_unaddressed=True), "ㅇㅇ", state) is True

    def test_꺼진_채널은_봇이_낀_스레드여도_안_받는다(
        self, policy: ResponsePolicy
    ) -> None:
        state = ThreadState(joined=True, bot_asked=True)
        assert policy.answers(채널(), "이거 해줘", state) is False
