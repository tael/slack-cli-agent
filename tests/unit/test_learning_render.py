"""학습 제안 렌더링.

원본 두 자리를 대응한다.
- bot.py 의 show_proposal() : 관리 명령 응답. 제목과 반영 안내가 붙는다
- learn.py 의 render() : 배치가 끝난 뒤 DM 으로 보내는 요약. 제목이 없고
  아무것도 못 뽑았을 때 note 를 붙인다
"""

from __future__ import annotations

from slack_cli_agent.learning.proposal import LearningProposal
from slack_cli_agent.learning.render import ProposalRenderer


def make_proposal(**overrides) -> LearningProposal:
    base = {
        "day": "2026-09-14",
        "writing_style": ("문장을 짧게 써라",),
        "channel_knowledge": {"공지": ("9월 회의는 매주 화요일이다",)},
        "corrections": ("재고 조회는 /stock 이다",),
        "note": "",
    }
    base.update(overrides)
    return LearningProposal(**base)


class TestRenderForDisplay:
    def test_제목과_반영_안내가_붙는다(self) -> None:
        text = ProposalRenderer().render_for_display(make_proposal())
        assert text.startswith("*2026-09-14 학습 제안*")
        assert "반영하시려면 `학습 반영` 이라고 해주세요." in text

    def test_형식_교정과_정정된_것이_채널_지식보다_먼저_나온다(self) -> None:
        text = ProposalRenderer().render_for_display(make_proposal())
        style_idx = text.index("*형식 교정*")
        corr_idx = text.index("*정정된 것*")
        channel_idx = text.index("*공지 확정 사실*")
        assert style_idx < corr_idx < channel_idx

    def test_항목이_불릿으로_나온다(self) -> None:
        text = ProposalRenderer().render_for_display(make_proposal())
        assert "- 문장을 짧게 써라" in text
        assert "- 9월 회의는 매주 화요일이다" in text
        assert "- 재고 조회는 /stock 이다" in text

    def test_빈_항목은_생략된다(self) -> None:
        empty = make_proposal(writing_style=(), corrections=(), channel_knowledge={})
        text = ProposalRenderer().render_for_display(empty)
        assert "*형식 교정*" not in text
        assert "*정정된 것*" not in text


class TestRenderSummary:
    def test_제목_없이_항목만_나온다(self) -> None:
        text = ProposalRenderer().render_summary(make_proposal())
        assert "학습 제안" not in text
        assert "*형식 교정*" in text
        assert "*공지 확정 사실*" in text
        assert "*정정된 것*" in text

    def test_형식_교정_채널_지식_정정된_것_순서다(self) -> None:
        text = ProposalRenderer().render_summary(make_proposal())
        style_idx = text.index("*형식 교정*")
        channel_idx = text.index("*공지 확정 사실*")
        corr_idx = text.index("*정정된 것*")
        assert style_idx < channel_idx < corr_idx

    def test_아무것도_없으면_안내_문구와_note를_붙인다(self) -> None:
        empty = make_proposal(
            writing_style=(), corrections=(), channel_knowledge={},
            note="오늘은 반응이 없었다",
        )
        text = ProposalRenderer().render_summary(empty)
        assert text == "오늘은 새로 배울 게 없었어요.\n오늘은 반응이 없었다"

    def test_아무것도_없고_note도_없으면_안내_문구만이다(self) -> None:
        empty = make_proposal(writing_style=(), corrections=(), channel_knowledge={}, note="")
        text = ProposalRenderer().render_summary(empty)
        assert text == "오늘은 새로 배울 게 없었어요."
