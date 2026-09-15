"""Daily learning batch: read history, analyze, save, apply, notify.

Guards against two failure modes: concurrent workers running the same day's
batch, and a notification failure turning an otherwise-successful apply into
a reported failure.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from .analyzer import ProposalBuilder
from .apply import LearningApplier
from .decoder import ChannelAnalysisResult
from .ports import ReactionSource, ResponseArchiveReader
from .progress import ChannelFailure, FailureKind, ProgressStore
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
    failures: tuple[ChannelFailure, ...] = ()


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
        progress: ProgressStore,
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
        self._progress = progress
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
        archives = self._archives.read_day(day)
        now = self._clock()
        progress = self._progress.load(day)
        # A channel the reader had to skip still counts as present, or its
        # progress is dropped as if it had no history (sca-b4o review).
        known = set(archives.texts) | archives.unreadable
        if known:
            progress = progress.restricted_to(known)
        elif progress.settled:
            # Mark done even with no history, or this date stays pending
            # forever. A day still holding an undelivered summary falls
            # through instead: a read error looks the same as no history, and
            # finishing here would drop that message (sca-b4o review).
            self._store.mark_done(day)
            return BatchReport(day=day, ran=False, reason="응답 기록이 없다", proposal=None)

        pending = progress.pending(archives.texts, now)
        # Recorded as failures rather than left out: a channel with no progress
        # entry lets the day settle, and it is then never read again.
        unreadable = tuple(
            ChannelFailure(channel=name, kind=FailureKind.ARCHIVE_UNREADABLE, detail="")
            for name in progress.pending(sorted(archives.unreadable), now)
        )

        if pending or unreadable:
            results: Mapping[str, ChannelAnalysisResult] = {}
            failures: tuple[ChannelFailure, ...] = unreadable
            if pending:
                # Only the outstanding channels are re-analyzed. Re-running the
                # whole day would re-apply and re-announce channels that already
                # succeeded (sca-b4o).
                round_result = self._builder.build(
                    day, {name: archives.texts[name] for name in pending},
                    self._collect(day, archives.texts),
                )
                results = round_result.results
                failures = round_result.failures + unreadable
            progress = progress.with_round(results, failures, now)
            # Written before applying: dying after apply with no record of the
            # finished channels would re-analyze them, and a reworded result
            # slips past the applier's duplicate check (sca-b4o review).
            self._progress.save(progress)

        proposal = LearningProposal.from_results(day, progress.completed)
        self._store.save(proposal)

        applied: Mapping[str, int] = {}
        if proposal.has_content:
            applied = self._applier.apply(proposal)
            if applied:
                self._store.mark_applied(day, applied)

        failures = tuple(progress.failures.values())
        notified = False
        fresh = progress.unannounced_results()
        # Applying runs on the whole day, so a round that only recovers an
        # earlier round's failed apply has nothing fresh — reporting the day's
        # results is what makes that message make sense (sca-b4o review).
        reportable = fresh if fresh else (progress.completed if applied else {})
        if reportable or progress.unnotified_failures():
            # The stored proposal covers the whole day, but announcing it again
            # would repeat the channels an earlier round already reported. What
            # went out is tracked on the progress record rather than taken from
            # this round, so a failed delivery is retried (sca-b4o review). The
            # failure list stays whole: it is the day's current state, not an
            # event log, and dropping the channels still stuck would read as if
            # they had recovered.
            notified = self._announce(
                day, LearningProposal.from_results(day, reportable), applied, failures,
            )
            # Only a delivered message counts. Recording it either way would
            # suppress that failure forever (sca-b4o review).
            if notified:
                progress = progress.with_reported()
                self._progress.save(progress)

        # Done only once nothing is worth retrying, and after the progress file
        # is on disk: the other order leaves a done day whose state is a round
        # behind (sca-b4o).
        if progress.settled:
            self._store.mark_done(day)

        return BatchReport(
            day=day, ran=bool(pending), reason="" if pending else "다시 분석할 채널이 없다",
            proposal=proposal, applied=applied, notified=notified, failures=failures,
        )

    def _collect(
        self, day: str, texts: Mapping[str, str],
    ) -> Mapping[str, Sequence[Mapping[str, object]]]:
        try:
            return self._reactions.collect(texts)
        except Exception as exc:  # noqa: BLE001 — a reaction fetch failure should not discard the day
            log.warning("%s 사람 반응을 모으지 못했다. 응답 기록만으로 분석한다 : %s", day, exc)
            return {}

    def _announce(
        self, day: str, proposal: LearningProposal, applied: Mapping[str, int],
        failures: tuple[ChannelFailure, ...],
    ) -> bool:
        text = self._render_text(day, proposal, applied, failures)
        try:
            return bool(self._notify(text))
        except Exception:  # noqa: BLE001 — apply already succeeded; a notify failure shouldn't fail the batch
            return False

    def _render_text(
        self, day: str, proposal: LearningProposal, applied: Mapping[str, int],
        failures: tuple[ChannelFailure, ...] = (),
    ) -> str:
        header = f"*{day} 학습*\n\n" if proposal.has_content else ""
        body = self._renderer.render_batch_summary(proposal, failures)
        if applied:
            where = ", ".join(f"{k} {v}건" for k, v in applied.items())
            footer = (
                f"\n\n지식에 반영했습니다. {where}\n"
                f"틀린 것이 있으면 `학습 되돌리기 {day}` 라고 알려주세요."
            )
        else:
            footer = ""
        return header + body + footer
