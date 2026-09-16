# A review that died with its process leaves only a "진행" row. Nothing else
# records it -- not the log, not the channel -- so the person who asked keeps
# waiting. This sweeps those rows into the troubleshooting channel and drops
# them, and only drops the ones it managed to report.

from __future__ import annotations

import logging
from datetime import UTC, datetime

from .base import PublisherPort
from .ledger import ReviewLedger, StaleReview

log = logging.getLogger(__name__)


class StaleReviewReporter:

    def __init__(self, ledger: ReviewLedger, publisher: PublisherPort, channel: str) -> None:
        self._ledger = ledger
        self._publisher = publisher
        self._channel = channel

    def sweep(self) -> int:
        if not self._channel:
            return 0
        reported = 0
        for stale in self._ledger.stale_in_progress():
            if self._report(stale):
                self._ledger.drop(stale.kind, stale.channel, stale.target_ts)
                reported += 1
        return reported

    def _report(self, stale: StaleReview) -> bool:
        try:
            posted = self._publisher.post(self._channel, None, self._text(stale), rich=False)
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the sweep
            log.warning("중단된 점검을 알리지 못했다 : %s %s", stale.target_ts, exc)
            return False
        return posted is not None

    @staticmethod
    def _text(stale: StaleReview) -> str:
        시작 = datetime.fromtimestamp(stale.at, UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        요청자 = stale.record.by or "알 수 없음"
        return (
            f"점검이 중단된 채로 남아 있었다 : {stale.kind}\n"
            f"- 채널 {stale.channel} / 대상 {stale.target_ts} / 요청 {요청자}\n"
            f"- 시작 {시작}\n"
            "프로세스 재기동으로 중단된 것으로 본다. 이모지를 다시 붙이면 점검이 새로 돈다."
        )
