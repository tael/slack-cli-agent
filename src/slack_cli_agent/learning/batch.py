"""학습 배치 흐름.

원본 learn.py 의 main() 순서를 그대로 옮긴다 — 응답 기록을 읽고, 반응을
모으고, 분석해 제안을 만들고, 저장하고, 반영하고, 소유자에게 알린다. 각
단계는 이미 있는 모듈(analyzer/proposal/apply/render)이 한다. 여기서 하는
일은 그 호출 순서와, 원본에는 없던 두 가지 안전장치뿐이다.

- 같은 날짜 배치를 워커 여럿이 동시에 돌리는 것을 막는다. 원본은 launchd
  타이머 하나만 이 배치를 불렀으므로 중복 실행을 고려하지 않았다.
- 알림 발송 실패가 그날 반영 자체를 실패로 만들지 않는다. 원본의
  slack() 호출은 예외를 그대로 던졌다 — 반영은 끝났는데 통신 오류
  때문에 배치 전체가 실패한 것처럼 보이는 문제가 있었다. 반응 조회
  실패는 원본에서도 collect_reactions() 안의 개별 try/except 로 이미
  막혀 있었다. 여기서는 그 계약을 ReactionSource.collect() 밖에서
  한 번 더 지킨다 — 구현이 그 안전장치를 안 지켜도 배치가 버틴다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from .analyzer import ProposalBuilder
from .apply import LearningApplier
from .ports import ReactionSource, ResponseArchiveReader
from .proposal import LearningProposal, ProposalStore
from .render import ProposalRenderer


@dataclass(frozen=True)
class BatchReport:
    """배치 한 번을 실행한 결과."""

    day: str
    ran: bool
    reason: str
    proposal: LearningProposal | None
    applied: Mapping[str, int] = field(default_factory=dict)
    notified: bool = False


class LearningBatch:
    """원본 main() 의 흐름 하나를 옮긴 것.

    자료 조회·분석·저장·반영·알림을 순서대로 부른다. 각 인자는 실물이
    아니라 계약(Protocol 이거나 이미 시험된 클래스)만 알면 된다 — 배치
    자체를 시험할 때 엔진과 슬랙 API 를 세울 필요가 없다.
    """

    def __init__(
        self,
        *,
        archives: ResponseArchiveReader,
        reactions: ReactionSource,
        builder: ProposalBuilder,
        store: ProposalStore,
        applier: LearningApplier,
        renderer: ProposalRenderer,
        notify: Callable[[str], None],
        clock: Callable[[], datetime],
    ) -> None:
        self._archives = archives
        self._reactions = reactions
        self._builder = builder
        self._store = store
        self._applier = applier
        self._renderer = renderer
        self._notify = notify
        self._clock = clock

    def run(self, day: str | None = None) -> BatchReport:
        day = day or self._clock().strftime("%Y-%m-%d")
        if not self._store.acquire_lock(day, now=self._clock()):
            return BatchReport(
                day=day,
                ran=False,
                reason="다른 작업이 이미 그날 배치를 돌리고 있다",
                proposal=None,
            )
        try:
            return self._run_locked(day)
        finally:
            self._store.release_lock(day)

    def _run_locked(self, day: str) -> BatchReport:
        channel_archives = self._archives.read_day(day)
        if not channel_archives:
            return BatchReport(day=day, ran=False, reason="응답 기록이 없다", proposal=None)

        try:
            reactions = self._reactions.collect(day)
        except Exception:  # noqa: BLE001 — 반응 조회 실패로 그날 학습 자체를 버리지 않는다
            reactions = {}

        proposal = self._builder.build(day, channel_archives, reactions)
        self._store.save(proposal)

        applied: Mapping[str, int] = {}
        if proposal.has_content:
            applied = self._applier.apply(proposal)
            if applied:
                self._store.mark_applied(day, applied)

        text = self._render_text(day, proposal, applied)
        notified = True
        try:
            self._notify(text)
        except Exception:  # noqa: BLE001 — 반영은 이미 끝났다. 알림 실패를 배치 실패로 만들지 않는다
            notified = False

        return BatchReport(
            day=day, ran=True, reason="", proposal=proposal, applied=applied, notified=notified,
        )

    def _render_text(
        self, day: str, proposal: LearningProposal, applied: Mapping[str, int],
    ) -> str:
        """원본 main() 의 DM 본문 조립과 같다."""
        header = f"*{day} 학습*\n\n" if proposal.has_content else ""
        body = self._renderer.render_summary(proposal)
        if applied:
            where = ", ".join(f"{k} {v}건" for k, v in applied.items())
            footer = (
                f"\n\n지식에 반영했습니다. {where}\n"
                f"틀린 것이 있으면 `학습 되돌리기 {day}` 라고 알려주세요."
            )
        else:
            footer = ""
        return header + body + footer
