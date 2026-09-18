"""Runs the watch job queue: checks registered jobs and handles the outcome.

Scheduling (calling `check_once()` periodically) and running the check
prompt itself (engine, permissions, working directory) are both the caller's
job, injected via `run_check`. This class only decides done/give-up/recheck
based on the response.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from ..config.channel import channel_is_rich
from ..config.settings import RuntimeSettings
from ..engine.base import EngineResponse
from ..guard.watch import WATCH_DONE_TAG, WATCH_STILL_TAG
from ..observability.audit import (
    WATCH_ABANDONED_KIND,
    WATCH_CHECKED_KIND,
    WATCH_FINISHED_KIND,
)
from ..slack.reactions import ReactionMarker
from .watchjobs import WatchJob, WatchJobPort
from .watchresult import WatchOutcome, WatchResultReader

log = logging.getLogger(__name__)


class RichFlag(Protocol):
    """Only field the completion report reads off a channel config."""

    @property
    def rich(self) -> bool: ...


class WatchChannelLookup(Protocol):
    """What the checker needs. ChannelRegistry satisfies it."""

    def get(self, channel_id: str) -> RichFlag | None: ...


class WatchReportPublisher(Protocol):
    """What the checker needs. MessagePublisher satisfies it."""

    def post(self, channel: str, thread_ts: str, text: str, rich: bool) -> str | None: ...

_TERMINAL = (WatchOutcome.SUCCEEDED, WatchOutcome.FAILED)


@runtime_checkable
class WatchAuditPort(Protocol):
    """AuditLog.record's shape."""

    def record(self, kind: str, *, channel: str = "", thread_ts: str = "", **fields: Any) -> None: ...


def watch_check_prompt(description: str, outcome: WatchOutcome = WatchOutcome.UNKNOWN,
                       result_path: str = "") -> str:
    """The prompt for one check turn.

    When the code already knows the work finished, the turn only writes the
    report — asking it to judge again would let a wrong tag override a read
    exit status. Only the undecidable case still goes through the tags.
    """
    if outcome in (WatchOutcome.SUCCEEDED, WatchOutcome.FAILED):
        결과 = "성공으로" if outcome is WatchOutcome.SUCCEEDED else "실패로"
        어디 = f"결과 파일 : {result_path}\n\n" if result_path else ""
        return (
            f"다음 백그라운드 작업이 {결과} 끝났다. 끝났는지는 이미 확인했으니 다시 판단하지 "
            "말고, 결과를 알리는 보고 문구만 평소 답변 형식으로 써라.\n\n"
            f"지켜보던 작업 : {description}\n\n"
            f"{어디}"
            "결과 파일이 있으면 읽고 무엇이 어떻게 됐는지 옮겨 적어라. 조회만 하고 새로 "
            "시키지 않는다."
        )
    return (
        "다음 백그라운드 작업이 끝났는지 지금 확인해라. 조회만 하고 새로 시키지 않는다.\n\n"
        f"지켜보는 작업 : {description}\n\n"
        "아직 끝나지 않았으면 다른 말 없이 답변 끝에 " + WATCH_STILL_TAG + " 만 붙여라.\n"
        "성공이든 실패든 끝났으면, 그 결과를 알리는 완료 보고 문구를 평소 답변 형식으로 쓰고 "
        "맨 끝에 " + WATCH_DONE_TAG + " 를 붙여라."
    )


class WatchJobChecker:
    """Processes one round of the watch queue.

    Give-up jobs are handled first and excluded from the check pass that
    follows, so a job that's just been given up doesn't also trigger a
    wasted engine call.
    """

    def __init__(
        self,
        *,
        queue: WatchJobPort,
        run_check: Callable[[WatchJob, WatchOutcome], EngineResponse],
        # None keeps the old tag-only judgement. Rows registered before the
        # result file was pinned have nothing to read either way.
        results: WatchResultReader | None = None,
        publisher: WatchReportPublisher,
        channels: WatchChannelLookup,
        settings: RuntimeSettings,
        reactions: ReactionMarker | None = None,
        # Return value is ignored here; object rather than None since some
        # callers care whether the send succeeded.
        notify_owner: Callable[[str], object] | None = None,
        # checks and last_run hold current state only, so without this there is
        # no record that a watch ever ran (sca-j3d).
        audit: WatchAuditPort | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._queue = queue
        self._run_check = run_check
        self._results = results
        self._publisher = publisher
        self._channels = channels
        self._settings = settings
        self._reactions = reactions
        self._notify_owner = notify_owner
        self._audit = audit
        self._now = now

    def check_once(self) -> None:
        now = self._now()

        handled_ids: set[int] = set()
        for job in self._queue.expired(
            now, self._settings.watch_job_max_age_sec, self._settings.watch_job_max_checks
        ):
            # An exit status already read outranks the clock: giving up on work
            # that finished drops its report and tells the owner it never ended.
            # The report has to go out here, not in the loop below -- the check
            # cap excludes the job there, so skipping left it open forever with
            # its report never posted (codex review).
            handled_ids.add(job.id)
            if self._outcome_of(job) in _TERMINAL:
                self._check_one(job, now)
                continue
            reason = _give_up_reason(job, self._settings.watch_job_max_checks)
            if self._notify_owner is not None:
                self._notify_owner(
                    _give_up_report(
                        job, reason, self._settings.watch_job_max_checks, now - job.created_at
                    )
                )
            self._queue.mark_done(job.id)
            self._settle_mark(job, failed=True)
            self._record(
                WATCH_ABANDONED_KIND, job, age_sec=now - job.created_at, reason=reason.value
            )

        for job in self._queue.due(
            now, self._settings.watch_job_min_gap_sec, self._settings.watch_job_max_checks
        ):
            if job.id in handled_ids:
                continue
            self._check_one(job, now)

        self._retry_held_reports()

    def _retry_held_reports(self) -> None:
        """Posts reports whose earlier send failed. No engine call: the check
        already ran and only the delivery is outstanding.
        """
        for held in self._queue.pending_reports():
            if self._post_report(held.job_id, held.channel, held.thread_ts, held.body):
                self._queue.release_report(held.job_id)

    def _post_report(self, job_id: int, channel: str, thread_ts: str, body: str) -> bool:
        config = self._channels.get(channel)
        try:
            self._publisher.post(channel, thread_ts, body, channel_is_rich(config))
        except Exception as exc:  # noqa: BLE001 — a post failure must not stop the job from being marked done
            # The body can carry Slack conversations or file contents, so the
            # log gets the identifiers only.
            log.error("감시 완료 보고 발송 실패 : 작업 %d, 채널 %s, %s", job_id, channel, exc)
            return False
        return True

    def _check_one(self, job: WatchJob, now: float) -> None:
        outcome = self._outcome_of(job)
        if outcome is WatchOutcome.RUNNING:
            # Nothing to ask: every check is an engine call, and the work has
            # not written its exit status yet.
            self._queue.mark_polled(job.id, now)
            return

        try:
            response = self._run_check(job, outcome)
        except Exception as exc:
            log.exception("감시 확인 중 오류 : 작업 %d, 확인 %d회", job.id, job.checks)
            self._queue.mark_checked(job.id, now)
            # The message can carry a path or a command; only the type is safe.
            self._record(
                WATCH_CHECKED_KIND, job, ok=False, exception_type=type(exc).__name__,
                outcome=outcome.name,
            )
            return

        self._record(
            WATCH_CHECKED_KIND, job, ok=response.ok, outcome=outcome.name,
            engine=response.engine, model=response.model_actual or "",
            elapsed=response.elapsed, turns=response.turns,
            failure=response.failure_reason or "", body_len=len(response.body),
        )

        if outcome in _TERMINAL:
            # The exit status decided this, not the reply. A failed engine call
            # still leaves the job open so the report gets another attempt.
            if response.ok:
                self._finish(job, response.body, outcome)
            else:
                log.warning(
                    "감시 완료 보고 생성 실패 : 작업 %d, 확인 %d회, 사유 %s",
                    job.id, job.checks, response.failure_reason or "미상",
                )
                self._queue.mark_checked(job.id, now)
            return

        if response.ok and WATCH_DONE_TAG in response.body:
            # Done wins over still, same as the original bot.py:6788. What the
            # original doesn't do is record the contradiction or strip the still
            # tag, so the raw marker ended up in the posted body.
            if WATCH_STILL_TAG in response.body:
                log.warning(
                    "감시 확인 응답의 태그가 모순된다. 완료로 처리한다 : 작업 %d, 확인 %d회",
                    job.id, job.checks,
                )
            self._finish(job, response.body)
            return

        # The body and the condition can carry Slack conversations or file
        # contents, so neither goes into the log. The two failures get separate
        # wording because they need different fixes.
        if not response.ok:
            log.warning(
                "감시 확인 엔진 실패 : 작업 %d, 확인 %d회, 사유 %s, 진단 %s, 응답 %d자",
                job.id, job.checks, response.failure_reason or "미상",
                str(response.failure_detail) or "없음", len(response.body),
            )
        elif WATCH_STILL_TAG not in response.body:
            log.warning(
                "감시 확인 태그 누락 : 작업 %d, 확인 %d회, 응답 %d자",
                job.id, job.checks, len(response.body),
            )
        self._queue.mark_checked(job.id, now)

    def _settle_mark(self, job: WatchJob, *, failed: bool) -> None:
        """Applies the final mark. The watch mark comes off inside that call —
        `STALE_ON_SETTLE` covers it — so there is no separate removal here to
        fail on its own and leave mag next to the final mark (sca-3p6).

        The give-up path goes through here too: its report only reaches the
        owner, so a message left marked reads as still being watched while the
        queue no longer has it. The original bot.py leaves the mark on.

        Callers close the queue first. Reactions are cosmetic, and an exception
        here must not put the job back in line for another owner report.
        """
        if self._reactions is None or not job.msg_ts:
            return
        try:
            if failed:
                self._reactions.mark_failed(job.channel, job.msg_ts)
            else:
                self._reactions.mark_done(job.channel, job.msg_ts)
        except Exception as exc:  # noqa: BLE001 — cosmetic; see module docstring
            log.error("감시 표식 정리 실패 : 작업 %d, 채널 %s, %s", job.id, job.channel, exc)

    def _outcome_of(self, job: WatchJob) -> WatchOutcome:
        if self._results is None:
            return WatchOutcome.UNKNOWN
        return self._results.read(job.workdir, job.run_id)

    def _finish(
        self, job: WatchJob, raw_body: str, outcome: WatchOutcome = WatchOutcome.UNKNOWN
    ) -> None:
        # A reply that is nothing but the done tag leaves an empty body, and an
        # empty body posts nothing at all. The job is marked done either way, so
        # without this the watch just disappears from the thread.
        body = _strip_tags(raw_body) or _bare_done_report(outcome)
        if not self._post_report(job.id, job.channel, job.thread_ts, body):
            # Marking done still happens -- rerunning the check would spend
            # another engine call on work already finished. The report is kept
            # so the next round retries only the send (sca-dlv).
            self._queue.hold_report(job.id, body)

        self._queue.mark_done(job.id)
        self._settle_mark(job, failed=False)
        self._record(WATCH_FINISHED_KIND, job, outcome=outcome.name)

    def _record(self, kind: str, job: WatchJob, **fields: Any) -> None:
        """Best effort by contract: a watch that already ran must not be lost
        because its record could not be written. The condition and the response
        body can carry Slack conversations or file contents, so neither goes in.
        """
        if self._audit is None:
            return
        try:
            self._audit.record(
                kind, channel=job.channel, thread_ts=job.thread_ts,
                watch_job_id=job.id, checks=job.checks, **fields,
            )
        except Exception as exc:  # noqa: BLE001 - see docstring
            log.warning("감시 실행 기록을 남기지 못했다 : %s", exc)


def _strip_tags(raw_body: str) -> str:
    """Removes every control tag. These are an engine-to-bot protocol and must
    not reach a channel, so the still tag is stripped too even though the done
    branch is the only caller.
    """
    body = raw_body
    for tag in (WATCH_DONE_TAG, WATCH_STILL_TAG):
        body = body.replace(tag, "")
    return body.strip()


def _bare_done_report(outcome: WatchOutcome = WatchOutcome.UNKNOWN) -> str:
    """Deliberately omits job.condition. The condition can carry content pulled
    from a linked thread or a file, and this goes to a channel thread — unlike
    the give-up report, which goes to the owner alone. The thread itself says
    what was being watched.

    A failed exit with an empty report would otherwise read as success.
    """
    머리 = (
        "*지켜보던 작업이 실패로 끝났습니다*"
        if outcome is WatchOutcome.FAILED
        else "*지켜보던 작업이 끝났습니다*"
    )
    return (
        f"{머리}\n\n"
        "확인 응답에 내용이 없어 결과를 옮기지 못했습니다. 직접 확인이 필요합니다."
    )


class GiveUpReason(StrEnum):
    """Why a watch stopped. expired() returns age and check-cap hits in one
    list, so the caller has to work this out itself (sca-uwq)."""

    MAX_CHECKS = "max_checks"
    MAX_AGE = "max_age"


def _give_up_reason(job: WatchJob, max_checks: int) -> GiveUpReason:
    """Which limit to report when both are over. The check cap is chosen by
    policy, not because it provably came first -- a short max_age or a late
    sweep can put the age over as well. It is the tighter limit at the default
    settings, and it is the one whose value the owner can act on."""
    if job.checks >= max_checks:
        return GiveUpReason.MAX_CHECKS
    return GiveUpReason.MAX_AGE


def _elapsed_text(age_sec: float) -> str:
    """Wording comes from the elapsed time, not from the 24-hour default. The
    limit is a setting, so a fixed "over a day" is wrong wherever it differs
    (codex review)."""
    if age_sec >= 3600:
        return f"{int(age_sec // 3600)}시간"
    if age_sec >= 60:
        return f"{int(age_sec // 60)}분"
    return f"{int(age_sec)}초"


def _give_up_report(job: WatchJob, reason: GiveUpReason, max_checks: int, age_sec: float) -> str:
    # The original bot.py:6808 has one line here because it has no check cap.
    # Reporting a four-hour stop as "over a day" would send the owner looking
    # at the wrong thing (sca-uwq).
    if reason is GiveUpReason.MAX_CHECKS:
        머리 = "*확인 횟수 상한에 닿은 감시 건이 있습니다*"
        횟수 = f"- 확인 횟수 : {job.checks}회 (상한 {max_checks}회)"
    else:
        머리 = f"*{_elapsed_text(age_sec)} 넘게 못 끝낸 감시 건이 있습니다*"
        횟수 = f"- 확인 횟수 : {job.checks}회"
    return (
        f"{머리}\n\n"
        f"- 무엇 : {job.condition}\n"
        f"- 채널 : {job.channel}\n"
        f"{횟수}\n\n"
        "감시를 여기서 멈춥니다. 직접 확인이 필요합니다."
    )
