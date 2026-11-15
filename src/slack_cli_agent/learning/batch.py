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
from .gate import CarryStore, LearningGate, count_entries, has_correction_signal
from .ports import DayArchives, ReactionSource, ResponseArchiveReader
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
    #: Channels left out of analysis this run, with the reason. Their record
    #: is on the carry list, not dropped.
    skipped: Mapping[str, str] = field(default_factory=dict)


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
        # Both or neither: without them every channel-day is analyzed.
        gate: LearningGate | None = None,
        carry: CarryStore | None = None,
    ) -> None:
        if (gate is None) != (carry is None):
            raise ValueError("gate 와 carry 는 함께 주어야 한다")
        self._gate = gate
        self._carry = carry
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
        carry = self._carry.load() if self._carry is not None else {}
        # A carried channel past its age limit is analyzed on a day with no
        # archive of its own, so it counts as present for that day.
        due = self._gate.due_channels(day, carry) if self._gate is not None else set()
        # A channel the reader had to skip still counts as present, or its
        # progress is dropped as if it had no history (sca-b4o review). One
        # already analyzed stays: its archive may be gone once the carry that
        # justified it is cleared, and dropping it would lose the result.
        known = set(archives.texts) | archives.unreadable | due | set(progress.completed)
        if known:
            progress = progress.restricted_to(known)
        elif progress.settled:
            # Mark done even with no history, or this date stays pending
            # forever. A day still holding an undelivered summary falls
            # through instead: a read error looks the same as no history, and
            # finishing here would drop that message (sca-b4o review).
            self._store.mark_done(day)
            return BatchReport(day=day, ran=False, reason="응답 기록이 없다", proposal=None)

        candidates = [*archives.texts, *sorted(due - set(archives.texts))]
        pending = progress.pending(candidates, now)
        analysis_texts: dict[str, str] = {name: archives.texts.get(name, "") for name in pending}
        skipped: dict[str, str] = {}
        merged: dict[str, tuple[str, ...]] = {}
        if self._gate is not None:
            pending, analysis_texts, skipped, merged = self._apply_gate(
                day, pending, archives, carry, progress.failures,
            )
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
                    day, {name: analysis_texts[name] for name in pending},
                    self._collect(day, {**archives.texts, **analysis_texts}),
                )
                results = round_result.results
                failures = round_result.failures + unreadable
            progress = progress.with_round(results, failures, now)
            # Written before applying: dying after apply with no record of the
            # finished channels would re-analyze them, and a reworded result
            # slips past the applier's duplicate check (sca-b4o review).
            self._progress.save(progress)
            self._clear_carried(results, merged)

        proposal = LearningProposal.from_results(day, progress.completed)
        # A day where every channel was carried over has nothing to record;
        # an empty proposal file would become the "latest" one.
        if progress.completed or not skipped:
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
        reportable = fresh or (progress.completed if applied else {})
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

        reason = ""
        if not pending:
            reason = self._idle_reason(skipped) if skipped else "다시 분석할 채널이 없다"
        return BatchReport(
            day=day, ran=bool(pending), reason=reason,
            proposal=proposal, applied=applied, notified=notified, failures=failures,
            skipped=skipped,
        )

    @staticmethod
    def _idle_reason(skipped: Mapping[str, str]) -> str:
        return "분석 기준 미달로 이월 : " + ", ".join(f"{name}({why})" for name, why in skipped.items())

    def _apply_gate(
        self, day: str, pending: Sequence[str], archives: DayArchives,
        carry: Mapping[str, Sequence[str]], failures: Mapping[str, ChannelFailure],
    ) -> tuple[tuple[str, ...], dict[str, str], dict[str, str], dict[str, tuple[str, ...]]]:
        """Splits the pending channels into those worth analyzing and those carried over.

        An analyzed channel's text is its carried days followed by today. A
        channel carried over is written to the carry list before this returns,
        so the day can be marked done without losing the record.
        """
        assert self._gate is not None and self._carry is not None
        analyze: list[str] = []
        texts: dict[str, str] = {}
        skipped: dict[str, str] = {}
        merged: dict[str, tuple[str, ...]] = {}
        for name in pending:
            earlier = tuple(d for d in carry.get(name, ()) if d < day)
            if day in carry.get(name, ()) and name not in failures:
                # Decided on an earlier tick of this day.
                skipped[name] = "이미 이월됨"
                continue
            today = archives.texts.get(name, "")
            carried_texts = self._read_carried(name, earlier)
            if carried_texts is None:
                skipped[name] = "이월 기록을 읽지 못해 보류"
                log.warning("%s %s 채널 분석 보류 : 이월된 날의 기록을 읽지 못했다 %s", day, name, earlier)
                if today:
                    self._carry.add(name, day)
                continue
            entries = count_entries(today) + sum(count_entries(t) for t in carried_texts.values())
            decision = self._gate.decide(
                day=day, carried_days=earlier, entries=entries, signal=has_correction_signal(today),
            )
            if not decision.analyze:
                skipped[name] = decision.reason
                self._carry.add(name, day)
                log.info("%s %s 채널 분석을 생략하고 이월한다 : %s", day, name, decision.reason)
                continue
            body = self._merge_days(day, today, carried_texts)
            if not body.strip():
                # Carried days whose archives are gone leave nothing to read.
                log.warning("%s %s 채널 이월 기록이 비어 있어 이월을 지운다 : %s", day, name, earlier)
                self._carry.clear(name, earlier)
                continue
            log.info("%s %s 채널을 분석한다 : %s", day, name, decision.reason)
            analyze.append(name)
            texts[name] = body
            merged[name] = earlier
        return tuple(analyze), texts, skipped, merged

    def _read_carried(self, name: str, days: Sequence[str]) -> dict[str, str] | None:
        """The carried days' text, or None when one exists but cannot be read.

        A day whose archive is simply gone yields empty text; an unreadable one
        must keep its carry entry, or its record would never be analyzed.
        """
        out: dict[str, str] = {}
        for carried_day in days:
            read = self._archives.read_day(carried_day)
            if name in read.unreadable:
                return None
            out[carried_day] = read.texts.get(name, "")
        return out

    @staticmethod
    def _merge_days(day: str, today: str, carried: Mapping[str, str]) -> str:
        if not any(carried.values()):
            return today
        parts = [f"# {d} 기록 (이월)\n{text}" for d, text in sorted(carried.items()) if text]
        if today:
            parts.append(f"# {day} 기록\n{today}")
        return "\n".join(parts)

    def _clear_carried(
        self, results: Mapping[str, ChannelAnalysisResult], merged: Mapping[str, tuple[str, ...]],
    ) -> None:
        """Drops carry entries only for channels whose analysis succeeded."""
        if self._carry is None:
            return
        for name in results:
            if merged.get(name):
                self._carry.clear(name, merged[name])

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
