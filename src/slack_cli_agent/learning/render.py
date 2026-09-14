"""학습 제안을 사람이 읽을 문장으로 바꾼다.

원본에 같은 데이터를 그리는 두 자리가 있었다.
- bot.py 의 ``show_proposal()`` — 관리 명령 응답. 제목과 반영 안내가 붙는다
- learn.py 의 ``render()`` — 배치 직후 DM. 제목이 없고 "형식 교정 → 채널
  지식 → 정정된 것" 순서였다(show_proposal 은 "형식 교정 → 정정된 것 →
  채널 지식" 순서였다). 같은 자료를 두 곳이 다른 순서로 그리는 것 자체가
  일관성 문제라 여기서는 각각의 원래 순서를 그대로 옮기고 메서드 이름으로
  구분한다.
"""

from __future__ import annotations

from .proposal import LearningProposal

_EMPTY_NOTICE = "오늘은 새로 배울 게 없었어요."


class ProposalRenderer:
    """제안 하나를 두 가지 형태로 그린다."""

    def render_for_display(self, proposal: LearningProposal) -> str:
        """관리 명령 `학습 제안` 응답. 원본 show_proposal() 과 같다."""
        lines = [f"*{proposal.day} 학습 제안*", ""]
        self._append_style_and_corrections(lines, proposal)
        self._append_channels(lines, proposal)
        lines.append("반영하시려면 `학습 반영` 이라고 해주세요.")
        return "\n".join(lines)

    def render_summary(self, proposal: LearningProposal) -> str:
        """배치 직후 DM 요약. 원본 learn.py 의 render() 와 같다."""
        lines: list[str] = []
        self._append_style_only(lines, proposal)
        self._append_channels(lines, proposal)
        self._append_corrections_only(lines, proposal)
        if not lines:
            lines.append(_EMPTY_NOTICE)
            if proposal.note:
                lines.append(proposal.note)
        return "\n".join(lines).strip()

    @staticmethod
    def _append_style_and_corrections(lines: list[str], proposal: LearningProposal) -> None:
        for label, items in (
            ("형식 교정", proposal.writing_style),
            ("정정된 것", proposal.corrections),
        ):
            if items:
                lines.append(f"*{label}*")
                lines += [f"- {x}" for x in items]
                lines.append("")

    @staticmethod
    def _append_style_only(lines: list[str], proposal: LearningProposal) -> None:
        if proposal.writing_style:
            lines.append("*형식 교정*")
            lines += [f"- {x}" for x in proposal.writing_style]
            lines.append("")

    @staticmethod
    def _append_corrections_only(lines: list[str], proposal: LearningProposal) -> None:
        if proposal.corrections:
            lines.append("*정정된 것*")
            lines += [f"- {x}" for x in proposal.corrections]
            lines.append("")

    @staticmethod
    def _append_channels(lines: list[str], proposal: LearningProposal) -> None:
        for channel, items in proposal.channel_knowledge.items():
            if items:
                lines.append(f"*{channel} 확정 사실*")
                lines += [f"- {x}" for x in items]
                lines.append("")
