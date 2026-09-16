"""정리 작업이 돌았다는 것을 기록한다.

지운 것이 있을 때만 로그를 남기면, 조용한 구간이 "지울 것이 없었다" 인지
"아예 안 돌았다" 인지 갈리지 않는다. 2026-09-15 에 job_purge 와 첨부 정리가
실제로 그 상태였다(sca-mf6).

그래서 0건도 남긴다. 다만 같은 줄만 반복되면 읽히지 않으므로 연속 0건 횟수와
이 프로세스가 사는 동안의 누적 삭제 수를 함께 적는다.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class SweepLog:

    def __init__(self, name: str) -> None:
        self._name = name
        self._idle_runs = 0
        self._total = 0

    def record(self, removed: int) -> None:
        self._total += removed
        if removed:
            self._idle_runs = 0
            log.info("%s 정리 %d건, 누적 %d건", self._name, removed, self._total)
            return
        self._idle_runs += 1
        log.info(
            "%s 정리 0건, 지울 것 없이 연속 %d회, 누적 %d건",
            self._name, self._idle_runs, self._total,
        )
