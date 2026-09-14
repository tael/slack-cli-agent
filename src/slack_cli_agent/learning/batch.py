"""Daily learning batch: read history, analyze, save, apply, notify.

Guards against two failure modes: concurrent workers running the same day's
batch, and a notification failure turning an otherwise-successful apply into
a reported failure.
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
    """Result of one batch run."""

    day: str
    ran: bool
    reason: str
    proposal: LearningProposal | None
    applied: Mapping[str, int] = field(default_factory=dict)
    notified: bool = False


class LearningBatch:
    """Runs read, analyze, save, apply, and notify in order.

    Depends only on protocols/interfaces so the batch can be tested without
    standing up a real engine or Slack API.
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
        # Returns whether the message actually reached someone; a wired-up
        # sender may fail by queuing for later rather than raising.
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
            # Mark done even with no history, or this date stays pending forever.
            self._store.mark_done(day)
            return BatchReport(day=day, ran=False, reason="응답 기록이 없다", proposal=None)

        try:
            reactions = self._reactions.collect(day)
        except Exception as exc:  # noqa: BLE001 — a reaction fetch failure should not discard the day
            log.warning("%s 사람 반응을 모으지 못했다. 응답 기록만으로 분석한다 : %s", day, exc)
            reactions = {}

        proposal = self._builder.build(day, channel_archives, reactions)
        self._store.save(proposal)

        applied: Mapping[str, int] = {}
        if proposal.has_content:
            applied = self._applier.apply(proposal)
            if applied:
                self._store.mark_applied(day, applied)

        # Mark done only after apply, so a failed apply doesn't get skipped as done.
        self._store.mark_done(day)

        text = self._render_text(day, proposal, applied)
        try:
            notified = bool(self._notify(text))
        except Exception:  # noqa: BLE001 — apply already succeeded; a notify failure shouldn't fail the batch
            notified = False

        return BatchReport(
            day=day, ran=True, reason="", proposal=proposal, applied=applied, notified=notified,
        )

    def _render_text(
        self, day: str, proposal: LearningProposal, applied: Mapping[str, int],
    ) -> str:
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
