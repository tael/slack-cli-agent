"""prompt/sections.py 의 is_silent() 시험.

기대 동작은 마메치 bot.py:3400-3425 의 is_silent() 와 동등해야 한다 — 본문
첫머리 접두 일치뿐 아니라, 본문 어디든 "독립된 줄 전체가 표식만" 이면
침묵으로 본다. 코드펜스(```) 안의 줄은 건너뛴다. 이중 대괄호 `[[침묵]]` 를
기본 표기로, 단일 대괄호 `[침묵]` 을 레거시로 계속 받는다(2026-10-19 통일).
"""

from __future__ import annotations

from pathlib import Path

from slack_cli_agent.auth.principal import Principal, TrustLevel
from slack_cli_agent.prompt.library import PromptLibrary
from slack_cli_agent.prompt.sections import (
    SILENT_MARK,
    SILENT_MARK_LEGACY,
    AskSection,
    CompositionContext,
    WorkStyleSection,
    is_silent,
)

_PRINCIPAL = Principal(user_id="U1", channel="C1", trust=TrustLevel.GENERAL, is_direct_message=False)


def _ctx(**overrides: object) -> CompositionContext:
    base: dict[str, object] = {
        "principal": _PRINCIPAL,
        # The package-bundled defaults are enough — ASK_NOTE ships there and
        # this test doesn't need a profile-specific override.
        "library": PromptLibrary(Path("/does/not/exist")),
    }
    base.update(overrides)
    return CompositionContext(**base)  # type: ignore[arg-type]


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


class TestAskSection:
    """[[ASK]] 표식 세팅 규약을 전하는 섹션. bot.py:1499-1507 상당.

    아직 어느 프로필의 섹션 목록에도 등록돼 있지 않다 — 등록은
    core/application.py 의 `_prompt_sections()` 를 손대야 하는데, 이번
    변경 범위 밖이다.
    """

    def test_항상_적용된다(self) -> None:
        section = AskSection()
        assert section.applies_to(_ctx()) is True

    def test_postmortem_에서는_빠진다(self) -> None:
        section = AskSection()
        assert section.applies_to(_ctx(postmortem=True)) is False

    def test_debug_trace_에서는_빠진다(self) -> None:
        section = AskSection()
        assert section.applies_to(_ctx(debug_trace=True)) is False

    def test_format_review_에서는_빠진다(self) -> None:
        section = AskSection()
        assert section.applies_to(_ctx(format_review=True)) is False

    def test_ASK_표식_설명을_담은_안내문을_낸다(self) -> None:
        section = AskSection()
        text = section.render(_ctx())
        assert "[[ASK]]" in text


class TestWorkStyleSection:
    """탐색, 스크립트 작성, 1회 실행, 결과 요약 순의 작업 방식 안내 (S1 턴 압축)."""

    def test_보통_턴에는_적용된다(self) -> None:
        assert WorkStyleSection().applies_to(_ctx()) is True

    def test_돌아보는_턴에서는_빠진다(self) -> None:
        for flag in ("postmortem", "debug_trace", "format_review"):
            assert WorkStyleSection().applies_to(_ctx(**{flag: True})) is False

    def test_스크립트_1회_실행과_묶어_부르기를_담는다(self) -> None:
        text = WorkStyleSection().render(_ctx())
        assert "스크립트" in text
        assert "1회 실행" in text
        assert "한 응답에 함께" in text

    def test_번호_조회는_정확_일치_컬럼_하나로_한다(self) -> None:
        # 2026-11-09 : 오더 조사가 여러 컬럼 OR, 앞 % LIKE 로 4억 행을 훑어 Bash 120초에 끊겼다.
        text = WorkStyleSection().render(_ctx())
        assert "컬럼 하나에 `=`" in text
        assert "OR" in text
        assert "기간 조건" in text

    def test_연관_조회는_한_Bash_에_묶는다(self) -> None:
        # 2026-11-09 : 오더 조사(T2)가 오더, 배정 이력, 이름 조회, SHOW COLUMNS 를 따로 불러 호출 14번이 들었다.
        # 묶으라는 줄을 더하자 T2 3회 중앙 호출 14 -> 10, 총 토큰 176만 -> 128만이 됐다.
        text = WorkStyleSection().render(_ctx())
        assert "한 Bash 에 모두 묶어" in text
        assert "SHOW COLUMNS" in text
        assert "JOIN" in text

    def test_짧게_유지한다(self) -> None:
        # 매 턴 다시 실리는 지침이라 몇 줄로 둔다. 지침 크기는 S2 과제가 따로 줄인다.
        # 800 에서 1200 으로 올렸다 : 번호 조회 줄(약 540 B, 150 토큰 안팎)이 더해졌다.
        # 1200 에서 1600 으로 올렸다 : 연관 조회 묶음 줄(약 370 B)이 더해졌다.
        # 1600 에서 1900 으로 올렸다 : 코드 용어 표기 줄(약 250 B, prompt/code_terms.py).
        text = WorkStyleSection().render(_ctx())
        assert len(text.encode("utf-8")) < 1900


def test_작업_방식_지침에_코드_용어_표기가_붙는다() -> None:
    # 2026-11-12: 교정 297문장 중 99개가 탈출구, 형제 호출이었다 (prompt/code_terms.py).
    text = WorkStyleSection().render(_ctx())
    assert "escape hatch 는 예외" in text
