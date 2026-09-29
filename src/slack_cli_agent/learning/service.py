"""Top-level entry point for showing, applying, and reverting proposals.

Each method returns a message string meant to be posted as-is. A missing
proposal and a corrupt one are reported differently, since ProposalStore
distinguishes them via Outcome.
"""

from __future__ import annotations

import re

from ..core.result import OutcomeKind
from .apply import LearningApplier, LearningReverter
from .proposal import ProposalStore
from .render import ProposalRenderer

_DAY_PATTERN = re.compile(r"\d{4}-\d\d-\d\d")


class LearningService:
    def __init__(
        self,
        store: ProposalStore,
        applier: LearningApplier,
        reverter: LearningReverter,
        renderer: ProposalRenderer,
    ) -> None:
        self._store = store
        self._applier = applier
        self._reverter = reverter
        self._renderer = renderer

    def show_proposal(self) -> str:
        outcome = self._store.latest()
        if outcome.kind is OutcomeKind.ABSENT:
            return "아직 학습 제안이 없어요."
        if outcome.kind is OutcomeKind.UNKNOWN:
            return f"학습 제안 파일을 읽지 못했어요. {outcome.reason}"
        return self._renderer.render_for_display(outcome.value())

    def apply_latest(self) -> str:
        # Safe to call even after the nightly batch already applied the
        # proposal — LearningApplier skips items already present.
        outcome = self._store.latest()
        if outcome.kind is OutcomeKind.ABSENT:
            return "반영할 학습 제안이 없어요."
        if outcome.kind is OutcomeKind.UNKNOWN:
            return f"학습 제안 파일을 읽지 못했어요. {outcome.reason}"
        proposal = outcome.value()
        try:
            done = self._applier.apply(proposal)
        except OSError as e:
            return f"반영하지 못했어요. {e}"
        if not done:
            return "새로 반영할 내용이 없어요. 이미 반영했거나 제안이 비어 있어요."
        self._store.mark_applied(proposal.day, done)
        where = "\n".join(f"- {k} {v}건" for k, v in done.items())
        return (
            f"{proposal.day} 에 배운 걸 반영했어요.\n{where}\n\n"
            f"틀린 게 있으면 `학습 되돌리기 {proposal.day}` 라고 알려주세요."
        )

    def revert(self, day: str) -> str:
        if not _DAY_PATTERN.fullmatch(day or ""):
            return "되분석할 날짜를 함께 주세요. 예 : 학습 되돌리기 2026-08-26"
        try:
            n = self._reverter.revert(day)
        except OSError as e:
            return f"되돌리지 못했어요. {e}"
        if not n:
            return f"{day} 에 반영된 게 없어요."
        return f"{day} 에 반영한 {n}줄을 지웠어요."
