"""수신자 표식 보조 함수 — pick_recipient_tag(A54)·missing_recipient_prompt(A53).

미표식 재요청 경로가 쓰는 순수 함수다. 재실행 답에서 표식 하나를 고르고
(복수/없음이면 거절), 표식을 다시 받기 위한 지시문을 돌려준다.
bot.py:1962-1983 missing_recipient_prompt / pick_recipient_tag 대조.
"""

from __future__ import annotations

from slack_cli_agent.core.markers import missing_recipient_prompt, pick_recipient_tag


class Test표식_고르기:
    def test_표식이_하나면_그_표식을_돌려준다(self) -> None:
        assert pick_recipient_tag("확인했습니다.\n[[RECIPIENT:OWNER]]") == "[[RECIPIENT:OWNER]]"

    def test_봇_표식도_아이디째_돌려준다(self) -> None:
        assert pick_recipient_tag("검토했습니다.\n[[RECIPIENT:BOT:U123ABC]]") == (
            "[[RECIPIENT:BOT:U123ABC]]"
        )

    def test_표식이_없으면_None(self) -> None:
        assert pick_recipient_tag("그냥 평범한 답변입니다.") is None

    def test_빈_입력도_None(self) -> None:
        assert pick_recipient_tag("") is None
        assert pick_recipient_tag(None) is None  # type: ignore[arg-type]

    def test_서로_다른_표식이_둘이면_None(self) -> None:
        text = "앞줄 [[RECIPIENT:OWNER]]\n뒷줄 [[RECIPIENT:ASKER]]"
        assert pick_recipient_tag(text) is None

    def test_같은_표식이_둘이면_그_표식을_돌려준다(self) -> None:
        text = "[[RECIPIENT:ASKER]] 그리고 또 [[RECIPIENT:ASKER]]"
        assert pick_recipient_tag(text) == "[[RECIPIENT:ASKER]]"


class Test재요청_지시문:
    def test_표식_세_선택지를_안내한다(self) -> None:
        prompt = missing_recipient_prompt()
        assert "[[RECIPIENT:ASKER]]" in prompt
        assert "[[RECIPIENT:OWNER]]" in prompt
        assert "RECIPIENT:BOT" in prompt

    def test_본문을_다시_쓰지_말라고_한다(self) -> None:
        # 재실행은 표식만 받는다. 본문이 바뀌면 앞 턴의 발송이 되풀이될 수 있다.
        prompt = missing_recipient_prompt()
        assert "본문" in prompt

    def test_조직_고정값을_담지_않는다(self) -> None:
        # 공통 골격이다. 특정 사람 이름이나 특정 봇 ID 를 박아 두지 않는다.
        prompt = missing_recipient_prompt()
        assert "김태일" not in prompt
