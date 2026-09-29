"""ResponsePolicy: 이름을 안 부른 스레드 답글에 나설지 판정한다."""

from __future__ import annotations

import pytest

from slack_cli_agent.config.channel import ChannelConfig
from slack_cli_agent.slack.gate import ResponseGate
from slack_cli_agent.slack.policy import (
    ResponsePolicy,
    ThreadState,
    addresses_someone_else,
)

아스카 = "<@U0EXAMPLE03>"


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


class TestAddressedToOther:
    """다른 참가자를 부른 메시지는 그 참가자의 것이다.

    2026-09-19 18:44 에 소유자가 아스카만 부른 요청을 이 봇이 가로챘다.
    같은 스레드에 봇이 여럿 있으면 "나를 안 불렀다" 가 "아무도 안 불렀다" 와
    다르다.
    """

    @pytest.mark.parametrize(
        "text",
        [
            f"{아스카} 리뷰해주세요",
            f"{아스카}\n리뷰해주세요",
            f"  {아스카}, 이거 해줘",
            "<@U0EXAMPLE03|아스카> 이거 해줘",
            f"{아스카} <@U0EXAMPLE04> 둘이 정리해주세요",
        ],
    )
    def test_선두_멘션은_다른_사람_몫이다(self, text: str) -> None:
        assert addresses_someone_else(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "이거 해줘",
            f"아까 {아스카} 가 말한 것 확인해줘",
            "<!here> 다들 확인해주세요",
            "",
        ],
    )
    def test_수신자_지정이_아니면_아니다(self, text: str) -> None:
        assert addresses_someone_else(text) is False

    def test_봇이_낀_스레드여도_다른_봇을_부르면_안_받는다(
        self, policy: ResponsePolicy
    ) -> None:
        state = ThreadState(joined=True, bot_asked=False)
        conf = 채널(answer_unaddressed=True)
        assert policy.answers(conf, f"{아스카} 리뷰해주세요", state) is False

    def test_되물은_직후여도_다른_봇을_부르면_안_받는다(
        self, policy: ResponsePolicy
    ) -> None:
        """quiet 과 bot_asked 보다 수신자 판정이 앞선다."""
        state = ThreadState(joined=True, bot_asked=True)
        for level in ("quiet", "normal", "active"):
            conf = 채널(answer_unaddressed=True, chat=level)
            assert policy.answers(conf, f"{아스카} 네 진행해", state) is False

    def test_참조로_나온_멘션은_그대로_받는다(self, policy: ResponsePolicy) -> None:
        state = ThreadState(joined=True, bot_asked=False)
        conf = 채널(answer_unaddressed=True)
        assert policy.answers(conf, f"아까 {아스카} 가 말한 것 확인해줘", state) is True


class TestChatLevel:
    """말수 설정이 판정 자체를 바꾼다. 지금까지는 프롬프트 문구에만 쓰였다."""

    def test_quiet은_되물은_답만_받는다(self, policy: ResponsePolicy) -> None:
        conf = 채널(answer_unaddressed=True, chat="quiet")
        용건 = ThreadState(joined=True, bot_asked=False)
        되물음답 = ThreadState(joined=True, bot_asked=True)
        assert policy.answers(conf, "이거 해줘", 용건) is False
        assert policy.answers(conf, "ㅇㅇ", 되물음답) is True

    def test_active는_봇이_안_낀_스레드도_받는다(self, policy: ResponsePolicy) -> None:
        conf = 채널(answer_unaddressed=True, chat="active")
        state = ThreadState(joined=False, bot_asked=False)
        assert policy.answers(conf, "이거 해줘", state) is True

    def test_active여도_감탄사만_있는_말은_안_받는다(self, policy: ResponsePolicy) -> None:
        conf = 채널(answer_unaddressed=True, chat="active")
        state = ThreadState(joined=False, bot_asked=False)
        assert policy.answers(conf, "감사합니다", state) is False

    def test_말수를_안_정한_채널은_normal과_같다(self, policy: ResponsePolicy) -> None:
        """기본값이 바뀌면 기존 채널의 응답 빈도가 조용히 달라진다."""
        state = ThreadState(joined=False, bot_asked=False)
        assert policy.answers(채널(answer_unaddressed=True), "이거 해줘", state) is False
        assert (
            policy.answers(채널(answer_unaddressed=True, chat="normal"), "이거 해줘", state)
            is False
        )
