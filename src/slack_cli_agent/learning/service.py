"""학습 제안 표시·반영·되돌리기의 상위 진입점.

원본 bot.py 의 show_proposal()/apply_learning()/revert_learning() 세 함수를
하나로 묶는다. 세 함수 모두 "사람에게 그대로 올릴 메시지 문자열"을 돌려주는
계약이었다 — admin/ 계층이 이 서비스 하나만 호출하면 그 계약이 그대로 유지된다.

원본과 다르게 만든 것 — 판정 불가(제안 파일이 깨짐)와 부재(제안이 없음)를
구분한다. 원본 show_proposal()/apply_learning()은 latest_proposal()의 두 실패
모드(파일 없음/파싱 실패)를 똑같이 "없다"로 봤다. 여기서는 ProposalStore가
Outcome으로 구분해 주므로, 깨진 파일이면 "읽지 못했다"는 별도 안내를 낸다.
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
        """원본 show_proposal() 과 같다."""
        outcome = self._store.latest()
        if outcome.kind is OutcomeKind.ABSENT:
            return "아직 학습 제안이 없어요."
        if outcome.kind is OutcomeKind.UNKNOWN:
            return f"학습 제안 파일을 읽지 못했어요. {outcome.reason}"
        return self._renderer.render_for_display(outcome.value())

    def apply_latest(self) -> str:
        """원본 apply_learning() 과 같다. 밤 배치가 이미 반영해도 이 명령은
        되짚어 넣는 용도라 같은 항목은 두 번 들어가지 않는다."""
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
        """원본 revert_learning() 과 같다."""
        if not _DAY_PATTERN.fullmatch(day or ""):
            return "되분석할 날짜를 함께 주세요. 예 : 학습 되돌리기 2026-08-26"
        try:
            n = self._reverter.revert(day)
        except OSError as e:
            return f"되돌리지 못했어요. {e}"
        if not n:
            return f"{day} 에 반영된 게 없어요."
        return f"{day} 에 반영한 {n}줄을 지웠어요."
