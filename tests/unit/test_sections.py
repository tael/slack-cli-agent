"""prompt/sections.py 의 is_silent() 시험.

기대 동작은 마메치 bot.py:3400-3425 의 is_silent() 와 동등해야 한다 — 본문
첫머리 접두 일치뿐 아니라, 본문 어디든 "독립된 줄 전체가 표식만" 이면
침묵으로 본다. 코드펜스(```) 안의 줄은 건너뛴다. 이중 대괄호 `[[침묵]]` 를
기본 표기로, 단일 대괄호 `[침묵]` 을 레거시로 계속 받는다(2026-10-19 통일).
"""

from __future__ import annotations

from slack_cli_agent.prompt.sections import SILENT_MARK, SILENT_MARK_LEGACY, is_silent


class Test표식_표기:
    def test_기본_표식은_이중_대괄호다(self) -> None:
        assert SILENT_MARK == "[[침묵]]"

    def test_레거시_표식은_단일_대괄호다(self) -> None:
        assert SILENT_MARK_LEGACY == "[침묵]"


class Test접두_일치:
    """본문 맨 앞이 표식이면 뒤에 사유가 붙어도 침묵이다."""

    def test_표식만_있으면_침묵이다(self) -> None:
        assert is_silent(SILENT_MARK) is True

    def test_레거시_표식만_있어도_침묵이다(self) -> None:
        assert is_silent(SILENT_MARK_LEGACY) is True

    def test_표식_뒤에_사유가_붙어도_침묵이다(self) -> None:
        assert is_silent(f"{SILENT_MARK} 부를 이유가 없어 보입니다") is True

    def test_앞뒤_공백은_무시한다(self) -> None:
        assert is_silent(f"\n\n  {SILENT_MARK}\n") is True


class Test줄단위_판정:
    """2026-10-02/19 보강: 본문 어디든 그 줄 전체가 표식만 있으면 침묵이다."""

    def test_설명_문장_뒤_독립된_줄의_표식도_침묵이다(self) -> None:
        body = (
            "이 호출은 메타몽을 부르는 말이에요. 마메치가 나설 자리가 아니에요.\n\n"
            f"{SILENT_MARK}"
        )
        assert is_silent(body) is True

    def test_레거시_표식도_독립된_줄이면_침묵이다(self) -> None:
        body = f"설명 문장입니다.\n{SILENT_MARK_LEGACY}"
        assert is_silent(body) is True

    def test_표식_줄_앞뒤_공백은_무시한다(self) -> None:
        body = f"설명입니다.\n   {SILENT_MARK}   \n"
        assert is_silent(body) is True

    def test_같은_줄에_다른_글자가_있으면_침묵이_아니다(self) -> None:
        body = f"답하지 않을 때는 {SILENT_MARK} 를 씁니다"
        assert is_silent(body) is False

    def test_표식을_언급만_하는_설명은_침묵이_아니다(self) -> None:
        """표식이 줄 맨 앞에 오면 접두 일치로 먼저 걸리므로, 본문 중간에
        인용하는 경우로 확인한다."""
        body = f"답하지 않을 때는 {SILENT_MARK} 표식을 쓴다."
        assert is_silent(body) is False


class Test코드펜스_예외:
    """코드블록 안에 표식을 예시로 적은 설명은 침묵이 아니다."""

    def test_코드블록_안의_표식_단독_줄은_침묵이_아니다(self) -> None:
        body = f"예시입니다.\n```\n{SILENT_MARK}\n```\n설명을 더 답니다."
        assert is_silent(body) is False

    def test_코드블록_밖의_표식_단독_줄은_여전히_침묵이다(self) -> None:
        body = f"```\n예시\n```\n\n{SILENT_MARK}"
        assert is_silent(body) is True

    def test_닫히지_않은_펜스_뒤는_코드로_본다(self) -> None:
        body = f"```\n{SILENT_MARK}"
        assert is_silent(body) is False


class Test빈_본문:
    def test_빈_문자열은_침묵이_아니다(self) -> None:
        assert is_silent("") is False

    def test_공백만_있어도_침묵이_아니다(self) -> None:
        assert is_silent("   \n  ") is False
