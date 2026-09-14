"""감시 큐 실행 — 등록된 감시 건을 실제로 확인하고 처리한다.

`watchjobs.py` 가 저장 계층(큐 자체)이고, 여기는 그 큐를 주기적으로 돌며
확인 프롬프트를 실행하고 결과에 따라 완료·포기·재확인을 가르는 실행 계층
이다. 원본 `bot.py` 의 `_watch_check_prompt()`, `job_watch()` 에 대응한다.

이 모듈이 맡지 않는 것 — 주기적으로 `check_once()` 를 부르는 것은 조립 코드
(스케줄러) 의 몫이다. 확인 프롬프트를 어떤 엔진·권한·작업 디렉터리로 돌릴지도
`run_check` 콜백으로 주입받으므로 이 클래스는 모른다. `WatchJob` 하나를
`run_check` 로 넘기고 그 응답을 보고 완료·포기·재확인을 가르는 것까지만
안다.
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
    """확인 프롬프트 문구. 원본 `_watch_check_prompt()` 를 그대로 옮겼다."""
    return (
        "다음 백그라운드 작업이 끝났는지 지금 확인해라. 조회만 하고 새로 시키지 않는다.\n\n"
        f"지켜보는 작업 : {description}\n\n"
        "아직 끝나지 않았으면 다른 말 없이 답변 끝에 " + WATCH_STILL_TAG + " 만 붙여라.\n"
        "성공이든 실패든 끝났으면, 그 결과를 알리는 완료 보고 문구를 평소 답변 형식으로 쓰고 "
        "맨 끝에 " + WATCH_DONE_TAG + " 를 붙여라."
    )


class WatchJobChecker:
    """감시 큐 한 회차 처리.

    포기 대상을 먼저 처리하고, 그 회차에서 포기 처리한 건은 이어지는 확인
    대상에서 뺀다 — 같은 건에 포기와 확인을 둘 다 하면 엔진을 헛되이 부른다.
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
        # 돌려주는 값은 보지 않는다. 발송 성공 여부를 쓰는 호출부가 있어
        # 반환형을 object 로 둔다 — 그 값을 여기서 판정하지 않는다.
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
        except Exception as exc:  # noqa: BLE001 — 감시 확인 중 예외로 워커 전체가 멎으면 안 된다 — 이 작업만 다음 회차로 넘긴다
            log.error("감시 확인 중 오류 : %s", exc)
            self._queue.mark_checked(job.id, now)
            return

        if response.ok and WATCH_DONE_TAG in response.body:
            self._finish(job, response.body)
            return

        if not response.ok or WATCH_STILL_TAG not in response.body:
            log.warning("감시 확인 응답이 기대한 형식이 아니다 : %s", job.condition[:80])
        self._queue.mark_checked(job.id, now)

    def _finish(self, job: WatchJob, raw_body: str) -> None:
        body = raw_body.replace(WATCH_DONE_TAG, "").strip()
        config = self._channels.get(job.channel)
        rich = bool(config and config.rich)
        try:
            self._publisher.post(job.channel, job.thread_ts, body, rich)
        except Exception as exc:  # noqa: BLE001 — 완료 보고 발송 실패가 완료 표시 자체를 막으면 안 된다 — 안 막으면 다음 회차에 같은 보고를 또 게시한다
            log.error("감시 완료 보고 발송 실패 : %s", exc)

        if self._reactions is not None and job.msg_ts:
            self._reactions.remove(job.channel, job.msg_ts, WATCH_MARK_EMOJI)
            self._reactions.mark_done(job.channel, job.msg_ts)

        # 발신·리액션이 실패해도 완료 표시는 한다. 여기서 안 끝내면 같은
        # 완료 보고를 다음 회차에 또 게시한다.
        self._queue.mark_done(job.id)


def _give_up_report(job: WatchJob) -> str:
    return (
        "*하루 넘게 못 끝낸 감시 건이 있습니다*\n\n"
        f"- 무엇 : {job.condition}\n"
        f"- 채널 : {job.channel}\n"
        f"- 확인 횟수 : {job.checks}회\n\n"
        "감시를 여기서 멈춥니다. 직접 확인이 필요합니다."
    )
