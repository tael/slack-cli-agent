"""`[[RECIPIENT:...]]` 표식 파싱과 미선언 `[[...]]` 표식 제거 시험.

마메치 bot.py:5373-5471 의 `RECIPIENT_TAG`/`take_recipient_tag`, bot.py:1551-1592 의
`CONTROL_TAG_LINE_RE`/`UNKNOWN_TAG_LINE_RE`/`strip_control_tags` 와 같은 동작을
`core.markers` 로 옮긴다. 수신자 쪽 멘션 강제(guard/mentions.py, B5 소유)는 여기
다루지 않는다 -- 이 모듈은 표식을 파싱해 `Recipient`/`RecipientResolution` 으로
건네줄 뿐이다.
"""

from __future__ import annotations

from slack_cli_agent.core.markers import (
    Recipient,
    RecipientResolution,
    resolve_recipient,
    strip_unknown_tags,
    take_recipient_tag,
)


class TestTakeRecipientTag:
    """bot.py:5425-5471 의 `take_recipient_tag` 와 같아야 한다."""

    def test_OWNER_표식을_떼고_분류한다(self) -> None:
        text, recipient = take_recipient_tag("보고 드립니다.\n[[RECIPIENT:OWNER]]")
        assert text == "보고 드립니다."
        assert recipient == Recipient(kind="owner")

    def test_ASKER_표식을_떼고_분류한다(self) -> None:
        text, recipient = take_recipient_tag("네, 맞습니다.\n[[RECIPIENT:ASKER]]")
        assert text == "네, 맞습니다."
        assert recipient == Recipient(kind="asker")

    def test_BOT_표식은_봇_ID_를_담는다(self) -> None:
        text, recipient = take_recipient_tag("검토 부탁한다.\n[[RECIPIENT:BOT:U0ACANBHY3U]]")
        assert text == "검토 부탁한다."
        assert recipient == Recipient(kind="bot", bot_id="U0ACANBHY3U")

    def test_표식이_없으면_그대로다(self) -> None:
        text, recipient = take_recipient_tag("그냥 답변입니다")
        assert text == "그냥 답변입니다"
        assert recipient is None

    def test_빈_문자열은_그대로다(self) -> None:
        text, recipient = take_recipient_tag("")
        assert text == ""
        assert recipient is None

    def test_문장_중간의_언급은_표식으로_안_본다(self) -> None:
        """줄 끝(또는 뒤따르는 다른 표식)까지 와야 표식이다 -- 규약을
        설명하는 문장은 지워지면 안 된다."""
        text, recipient = take_recipient_tag("[[RECIPIENT:OWNER]] 표식은 이렇게 쓴다.")
        assert recipient is None
        assert text == "[[RECIPIENT:OWNER]] 표식은 이렇게 쓴다."

    def test_정의되지_않은_값은_표식으로_안_본다(self) -> None:
        text, recipient = take_recipient_tag("답변입니다.\n[[RECIPIENT:FOO]]")
        assert recipient is None
        assert text == "답변입니다.\n[[RECIPIENT:FOO]]"

    def test_뒤따르는_다른_표식은_되돌려_붙인다(self) -> None:
        """뒤 단계의 NOWATCH/ASK 판정이 그 줄을 봐야 하므로, RECIPIENT 표식
        뒤에 붙은 다른 표식 줄은 지우지 않고 그대로 둔다 (bot.py:5441 주석)."""
        text, recipient = take_recipient_tag(
            "정리해 드렸습니다.\n[[RECIPIENT:OWNER]]\n[[NOWATCH]]"
        )
        assert recipient == Recipient(kind="owner")
        assert text == "정리해 드렸습니다.\n[[NOWATCH]]"


class TestResolveRecipient:
    """엔진이 게시 전 호출할 상위 함수 -- 미표식 차단은 플래그 뒤에 있다."""

    def test_기본값은_차단하지_않는다(self) -> None:
        resolution = resolve_recipient("표식 없는 답변입니다")
        assert resolution == RecipientResolution(
            text="표식 없는 답변입니다", recipient=None, blocked=False
        )

    def test_플래그를_켜면_미표식_답변을_차단한다(self) -> None:
        resolution = resolve_recipient("표식 없는 답변입니다", require_recipient=True)
        assert resolution.recipient is None
        assert resolution.blocked is True

    def test_플래그를_켜도_표식이_있으면_차단하지_않는다(self) -> None:
        resolution = resolve_recipient(
            "확인 부탁드립니다.\n[[RECIPIENT:OWNER]]", require_recipient=True
        )
        assert resolution.recipient == Recipient(kind="owner")
        assert resolution.blocked is False
        assert resolution.text == "확인 부탁드립니다."

    def test_플래그가_꺼져_있으면_표식이_있어도_정상_분류한다(self) -> None:
        resolution = resolve_recipient("고마워요.\n[[RECIPIENT:ASKER]]")
        assert resolution.recipient == Recipient(kind="asker")
        assert resolution.blocked is False


class TestStripUnknownTags:
    """bot.py:1564-1592 의 `strip_control_tags` 중 '미선언 표식' 절반을 맡는다.

    알려진 표식(ASK/NOWATCH/WATCH/RECIPIENT/침묵)은 각자의 take_*_tag 가
    따로 처리하므로 여기서는 손대지 않고 그대로 둔다. 그 목록에 없는
    `[[...]]` 단독 줄만 제거하고 기록한다.
    """

    def test_미선언_표식_단독_줄을_떼고_기록한다(self) -> None:
        text, dropped = strip_unknown_tags("답변입니다.\n[[테스트]]")
        assert text == "답변입니다."
        assert dropped == ["unknown:[[테스트]]"]

    def test_표식이_없으면_그대로다(self) -> None:
        text, dropped = strip_unknown_tags("그냥 답변입니다")
        assert text == "그냥 답변입니다"
        assert dropped == []

    def test_빈_문자열은_그대로다(self) -> None:
        text, dropped = strip_unknown_tags("")
        assert text == ""
        assert dropped == []

    def test_알려진_표식은_건드리지_않는다(self) -> None:
        body = (
            "본문입니다.\n"
            "[[ASK]]\n"
            "[[NOWATCH]]\n"
            "[[WATCH: 나중에 확인할 대상]]\n"
            "[[RECIPIENT:OWNER]]\n"
            "[[침묵]]"
        )
        text, dropped = strip_unknown_tags(body)
        assert text == body
        assert dropped == []

    def test_문장_중간의_대괄호는_건드리지_않는다(self) -> None:
        text, dropped = strip_unknown_tags("본문에 [[테스트]] 라는 말이 섞여 있다.")
        assert text == "본문에 [[테스트]] 라는 말이 섞여 있다."
        assert dropped == []

    def test_코드블록_안의_예시는_건드리지_않는다(self) -> None:
        body = "표식 예시입니다.\n```\n[[테스트]]\n```"
        text, dropped = strip_unknown_tags(body)
        assert text == body
        assert dropped == []

    def test_여러_줄의_미선언_표식을_각각_기록한다(self) -> None:
        text, dropped = strip_unknown_tags("[[하나]]\n본문\n[[둘]]")
        assert text == "본문"
        assert dropped == ["unknown:[[하나]]", "unknown:[[둘]]"]
