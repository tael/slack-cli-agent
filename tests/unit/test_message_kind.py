"""MessageKind — 슬랙 메시지를 무엇으로 볼지 한 곳에서 판정한다.

같은 판정이 네 곳에 따로 적혀 있었고 기준이 갈렸다. 소켓 이벤트를 받는
`EventListener` 는 파일을 붙여 보낸 말(`subtype="file_share"`)을 사람이 새로
건넨 말로 받는데, 캐치업·추가 발언 수집·대화 기록은 `subtype` 이 있으면 무조건
걸렀다. 그래서 파일을 붙여 부른 요청은 소켓으로 들어올 때만 처리되고, 재기동
중에 들어오면 캐치업이 못 잡아 유실된다.
"""

from __future__ import annotations

from slack_cli_agent.slack.message_kind import MessageKind


class Test사람이건넨말:
    def test_subtype_이_없으면_사람_말이다(self) -> None:
        assert MessageKind().is_human({"user": "U1", "text": "안녕"})

    def test_파일을_붙인_말도_사람_말이다(self) -> None:
        """슬랙은 파일을 붙이면 subtype 을 붙인다. 그것으로 거르면 그 요청이 유실된다."""
        assert MessageKind().is_human({"user": "U1", "subtype": "file_share", "text": "이거 봐줘"})

    def test_채널_참여_알림은_사람_말이_아니다(self) -> None:
        assert not MessageKind().is_human({"user": "U1", "subtype": "channel_join"})

    def test_봇이_올린_말은_사람_말이_아니다(self) -> None:
        assert not MessageKind().is_human({"bot_id": "B1", "text": "답변"})

    def test_봇이_올린_파일도_사람_말이_아니다(self) -> None:
        assert not MessageKind().is_human({"bot_id": "B1", "subtype": "file_share"})


class Test기록에넣을말:
    """대화 기록은 봇의 답도 포함한다. 사람 말만 남기면 문맥이 끊긴다."""

    def test_사람_말을_넣는다(self) -> None:
        assert MessageKind().is_transcribable({"user": "U1", "text": "안녕"})

    def test_봇_말을_넣는다(self) -> None:
        assert MessageKind().is_transcribable({"bot_id": "B1", "text": "답변"})

    def test_파일을_붙인_사람_말을_넣는다(self) -> None:
        assert MessageKind().is_transcribable({"user": "U1", "subtype": "file_share", "text": "이거"})

    def test_채널_참여_알림은_안_넣는다(self) -> None:
        assert not MessageKind().is_transcribable({"user": "U1", "subtype": "channel_join"})


class Test받아들이는_subtype을_바꿀_수_있다:
    def test_목록을_주면_그것을_쓴다(self) -> None:
        kind = MessageKind(human_subtypes=frozenset({"file_share", "thread_broadcast"}))
        assert kind.is_human({"user": "U1", "subtype": "thread_broadcast"})

    def test_기본_목록에_없으면_거른다(self) -> None:
        assert not MessageKind().is_human({"user": "U1", "subtype": "message_changed"})


class Test받아들이는_subtype_목록은_한_벌이다:
    """A15. 같은 목록이 message_kind 와 guard/mentions 에 따로 있었고 갈렸다.

    message_kind 는 file_share 만, mentions 는 thread_broadcast 까지 받았다.
    "채널에도 보내기" 로 올린 스레드 답글은 소켓 접수와 캐치업에서 버려지고
    호명 판정에서만 사람 말로 세어졌다. 마메치 bot.py HUMAN_SUBTYPES 는 두
    subtype 을 모두 받는다.
    """

    def test_채널에도_보낸_스레드_답글은_사람_말이다(self) -> None:
        assert MessageKind().is_human(
            {"user": "U1", "subtype": "thread_broadcast", "thread_ts": "1.0", "text": "이것도"}
        )

    def test_호명_판정과_같은_상수를_쓴다(self) -> None:
        from slack_cli_agent.guard import mentions
        from slack_cli_agent.slack import message_kind

        assert mentions.HUMAN_SUBTYPES is message_kind.HUMAN_SUBTYPES

    def test_마메치와_같은_목록이다(self) -> None:
        from slack_cli_agent.slack.message_kind import HUMAN_SUBTYPES

        assert frozenset({"file_share", "thread_broadcast"}) == HUMAN_SUBTYPES
