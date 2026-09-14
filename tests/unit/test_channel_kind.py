"""채널 ID 로 DM 여부를 판정한다.

같은 판정(`channel.startswith("D")`)이 `auth/policy.py`, `slack/publisher.py`,
`slack/participants.py`, `core/application.py` 네 곳에 따로 적혀 있었다.
슬랙 채널 ID 규칙(선행 문자로 종류를 가른다)을 코드 곳곳이 직접 아는 셈이라,
그 규칙이 바뀌면 네 곳을 다 찾아 고쳐야 했다. 판정을 여기 하나로 모은다.
"""

from __future__ import annotations

from slack_cli_agent.core.channel_kind import is_direct_message_channel


class TestDM여부판정:
    def test_D로_시작하면_DM이다(self) -> None:
        assert is_direct_message_channel("D12345")

    def test_C로_시작하면_DM이_아니다(self) -> None:
        assert not is_direct_message_channel("C12345")

    def test_G로_시작하는_그룹_DM도_DM이_아니다(self) -> None:
        """원본 기준을 그대로 옮긴다 — 선행 문자가 D 인 것만 DM 으로 본다."""
        assert not is_direct_message_channel("G12345")

    def test_빈_문자열은_DM이_아니다(self) -> None:
        assert not is_direct_message_channel("")
