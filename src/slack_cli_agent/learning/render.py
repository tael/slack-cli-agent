"""Renders a learning proposal as human-readable text.

Two renderings exist because the admin-command view and the batch DM
historically ordered sections differently (writing style / corrections /
channel knowledge vs. writing style / channel knowledge / corrections); this
keeps each order under its own method name rather than unifying them.
"""

from __future__ import annotations

from collections.abc import Sequence

from .progress import ChannelFailure
from .proposal import LearningProposal

_EMPTY_NOTICE = "오늘은 새로 배울 게 없었어요."


class ProposalRenderer:
    def render_for_display(self, proposal: LearningProposal) -> str:
        lines = [f"*{proposal.day} 학습 제안*", ""]
        self._append_style_and_corrections(lines, proposal)
        self._append_channels(lines, proposal)
        lines.append("반영하시려면 `학습 반영` 이라고 해주세요.")
        return "\n".join(lines)

    def render_summary(self, proposal: LearningProposal) -> str:
        lines: list[str] = []
        self._append_style_only(lines, proposal)
        self._append_channels(lines, proposal)
        self._append_corrections_only(lines, proposal)
        if not lines:
            lines.append(_EMPTY_NOTICE)
            if proposal.note:
                lines.append(proposal.note)
        return "\n".join(lines).strip()

    def render_batch_summary(
        self, proposal: LearningProposal, failures: Sequence[ChannelFailure],
    ) -> str:
        """The batch DM. Failures show regardless of whether anything was picked —
        a channel that failed used to disappear whenever another one succeeded
        (sca-b4o).
        """
        body = self.render_summary(proposal)
        if not failures:
            return body
        lines = ["*분석하지 못한 채널*"]
        lines += [f"- {f.channel} : {f.kind.description}" for f in failures]
        return f"{body}\n\n" + "\n".join(lines)

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
