"""학습 배치 흐름.

원본 learn.py 의 main() 순서를 그대로 옮긴다 — 응답 기록을 읽고, 반응을
모으고, 분석해 제안을 만들고, 저장하고, 반영하고, 소유자에게 알린다. 각
단계는 이미 있는 모듈(analyzer/proposal/apply/render)이 한다. 여기서 하는
일은 그 호출 순서와, 원본에는 없던 두 가지 안전장치뿐이다.

- 같은 날짜 배치를 워커 여럿이 동시에 실행하는 것을 막는다. 원본은 launchd
  타이머 하나만 이 배치를 불렀으므로 중복 실행을 고려하지 않았다.
- 알림 발송 실패가 그날 반영 자체를 실패로 만들지 않는다. 원본의
  slack() 호출은 예외를 그대로 던졌다 — 반영은 끝났는데 통신 오류
  때문에 배치 전체가 실패한 것처럼 보이는 문제가 있었다. 반응 조회
  실패는 원본에서도 collect_reactions() 안의 개별 try/except 로 이미
  막혀 있었다. 여기서는 그 계약을 ReactionSource.collect() 밖에서
  한 번 더 지킨다 — 구현이 그 안전장치를 안 지켜도 배치가 버틴다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from .analyzer import ProposalBuilder
from .apply import LearningApplier
from .ports import ReactionSource, ResponseArchiveReader
from .proposal import LearningProposal, ProposalStore
from .render import ProposalRenderer

log = logging.getLogger(__name__)


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
        # 발송기. 지금 사람에게 닿았으면 True 를 돌려준다 — 조립의 발송기는
        # 실패를 예외가 아니라 보류 저장으로 처리하므로, 예외만 보면 즉시
        # 발송 실패가 보고에서 성공으로 읽힌다.
        notify: Callable[[str], bool],
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
            # 기록이 없는 날도 끝난 것으로 표시한다. 안 하면 그 날짜가 계속
            # 미완료로 남아 주기마다 다시 집힌다.
            self._store.mark_done(day)
            return BatchReport(day=day, ran=False, reason="응답 기록이 없다", proposal=None)

        try:
            reactions = self._reactions.collect(day)
        except Exception as exc:  # noqa: BLE001 — 반응 조회 실패로 그날 학습 자체를 버리지 않는다
            log.warning("%s 사람 반응을 모으지 못했다. 응답 기록만으로 분석한다 : %s", day, exc)
            reactions = {}

        proposal = self._builder.build(day, channel_archives, reactions)
        self._store.save(proposal)

        applied: Mapping[str, int] = {}
        if proposal.has_content:
            applied = self._applier.apply(proposal)
            if applied:
                self._store.mark_applied(day, applied)

        # 반영까지 끝난 뒤에 표시한다. 저장 직후에 표시하면 반영이 실패한 날을
        # 다음 주기가 끝난 것으로 보고 넘어간다.
        self._store.mark_done(day)

        text = self._render_text(day, proposal, applied)
        try:
            notified = bool(self._notify(text))
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
