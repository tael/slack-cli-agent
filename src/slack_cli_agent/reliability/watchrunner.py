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

from ..config.channel import ChannelRegistry
from ..config.settings import RuntimeSettings
from ..engine.base import EngineResponse
from ..guard.watch import WATCH_DONE_TAG, WATCH_MARK_EMOJI, WATCH_STILL_TAG
from ..slack.publisher import MessagePublisher
from ..slack.reactions import ReactionMarker
from .watchjobs import WatchJob, WatchJobPort

log = logging.getLogger(__name__)


def watch_check_prompt(description: str) -> str:
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
        run_check: Callable[[WatchJob], EngineResponse],
        publisher: MessagePublisher,
        channels: ChannelRegistry,
        settings: RuntimeSettings,
        reactions: ReactionMarker | None = None,
        # Return value is ignored here; object rather than None since some
        # callers care whether the send succeeded.
        notify_owner: Callable[[str], object] | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._queue = queue
        self._run_check = run_check
        self._publisher = publisher
        self._channels = channels
        self._settings = settings
        self._reactions = reactions
        self._notify_owner = notify_owner
        self._now = now

    def check_once(self) -> None:
        now = self._now()

        given_up_ids: set[int] = set()
        for job in self._queue.expired(now, self._settings.watch_job_max_age_sec):
            given_up_ids.add(job.id)
            if self._notify_owner is not None:
                self._notify_owner(_give_up_report(job))
            self._queue.mark_done(job.id)

        for job in self._queue.due(now, self._settings.watch_job_min_gap_sec):
            if job.id in given_up_ids:
                continue
            self._check_one(job, now)

    def _check_one(self, job: WatchJob, now: float) -> None:
        try:
            response = self._run_check(job)
        except Exception:
            log.exception("감시 확인 중 오류 : 작업 %d, 확인 %d회", job.id, job.checks)
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
                "감시 확인 엔진 실패 : 작업 %d, 확인 %d회, 사유 %s, 응답 %d자",
                job.id, job.checks, response.failure_reason or "미상", len(response.body),
            )
        elif WATCH_STILL_TAG not in response.body:
            log.warning(
                "감시 확인 태그 누락 : 작업 %d, 확인 %d회, 응답 %d자",
                job.id, job.checks, len(response.body),
            )
        self._queue.mark_checked(job.id, now)

    def _finish(self, job: WatchJob, raw_body: str) -> None:
        # A reply that is nothing but the done tag leaves an empty body, and an
        # empty body posts nothing at all. The job is marked done either way, so
        # without this the watch just disappears from the thread.
        body = _strip_tags(raw_body) or _bare_done_report()
        config = self._channels.get(job.channel)
        rich = bool(config and config.rich)
        try:
            self._publisher.post(job.channel, job.thread_ts, body, rich)
        except Exception as exc:  # noqa: BLE001 — a post failure shouldn't block marking done, or the same report reposts next round
            log.error("감시 완료 보고 발송 실패 : 작업 %d, 채널 %s, %s", job.id, job.channel, exc)

        if self._reactions is not None and job.msg_ts:
            self._reactions.remove(job.channel, job.msg_ts, WATCH_MARK_EMOJI)
            self._reactions.mark_done(job.channel, job.msg_ts)

        self._queue.mark_done(job.id)


def _strip_tags(raw_body: str) -> str:
    """Removes every control tag. These are an engine-to-bot protocol and must
    not reach a channel, so the still tag is stripped too even though the done
    branch is the only caller.
    """
    body = raw_body
    for tag in (WATCH_DONE_TAG, WATCH_STILL_TAG):
        body = body.replace(tag, "")
    return body.strip()


def _bare_done_report() -> str:
    """Deliberately omits job.condition. The condition can carry content pulled
    from a linked thread or a file, and this goes to a channel thread — unlike
    the give-up report, which goes to the owner alone. The thread itself says
    what was being watched.
    """
    return (
        "*지켜보던 작업이 끝났습니다*\n\n"
        "확인 응답에 내용이 없어 결과를 옮기지 못했습니다. 직접 확인이 필요합니다."
    )


def _give_up_report(job: WatchJob) -> str:
    return (
        "*하루 넘게 못 끝낸 감시 건이 있습니다*\n\n"
        f"- 무엇 : {job.condition}\n"
        f"- 채널 : {job.channel}\n"
        f"- 확인 횟수 : {job.checks}회\n\n"
        "감시를 여기서 멈춥니다. 직접 확인이 필요합니다."
    )
