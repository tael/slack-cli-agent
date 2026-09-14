"""프로세스 상태 스냅샷을 파일로 내려 적는 계층.

원본 `bot.py` 의 `state_snapshot()`(대기줄·소켓 오류 이력·되짚기 대기 등
프로세스 메모리에만 있는 값을 사전으로 만듦)과 `write_state_snapshot()`(그
사전을 원자적으로 파일에 갈아 끼움)을 이관했다. 원본은 전역 변수를 직접
읽었는데, 그대로 옮기면 이 모듈이 그 전역 상태에 매인다. 그래서 값을 제공하는
쪽을 `SnapshotSource` 뒤로 감춰 주입받는 구조로 바꿨다.

기록이 실패해도 요청 처리에 영향이 없어야 한다 — `StateSnapshotWriter.write()`
는 어떤 예외도 밖으로 내지 않는다. 주기 실행이 필요하면 이 모듈이 아니라
`core.periodic.PeriodicRunner` 에 `write` 를 작업으로 넘긴다.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

# 소켓 오류·재접속을 "최근"으로 볼 창. 원본의 3분 창을 그대로 옮겼다.
RECENT_WINDOW_SEC = 180.0


class SnapshotSource(Protocol):
    """스냅샷에 담을 값을 제공한다.

    이 프로토콜을 구현하는 쪽이 실제 대기줄·소켓 오류 이력 등을 어디서
    가져오는지 안다. `StateSnapshotBuilder` 는 그 출처를 모른 채로 값만 받는다.
    """

    def inflight_count(self) -> int: ...

    def queued_threads(self) -> Mapping[str, int]:
        """스레드 ts 별 대기 중인 항목 수."""
        ...

    def socket_error_timestamps(self) -> Sequence[float]: ...

    def socket_reconnect_timestamps(self) -> Sequence[float]: ...

    def catchup_pending_count(self) -> int: ...

    def watch_job_count(self) -> int | None:
        """등록된 감시 작업 수. 조회에 실패했으면 `None`."""
        ...

    def is_shutting_down(self) -> bool: ...

    def started_at(self) -> float: ...


class StateSnapshotBuilder:
    """`SnapshotSource` 가 내놓는 값으로 스냅샷 사전 하나를 만든다."""

    def __init__(
        self,
        source: SnapshotSource,
        *,
        now: Callable[[], float] = time.time,
        pid: Callable[[], int] = os.getpid,
    ) -> None:
        self._source = source
        self._now = now
        self._pid = pid

    def build(self) -> dict[str, Any]:
        now = self._now()
        errors = list(self._source.socket_error_timestamps())
        reconnects = list(self._source.socket_reconnect_timestamps())
        # 항목 수가 0인 대기줄은 뺀다 — 실제로 기다리는 것이 없는 스레드다.
        queued = {ts: count for ts, count in self._source.queued_threads().items() if count}
        started_at = self._source.started_at()
        return {
            "written_at": now,
            "pid": self._pid(),
            "started_at": started_at,
            "uptime_sec": now - started_at,
            "shutting_down": self._source.is_shutting_down(),
            "inflight": self._source.inflight_count(),
            "queued_threads": len(queued),
            "queued_total": sum(queued.values()),
            "queued": queued,
            "socket_errors_3min": sum(1 for x in errors if now - x <= RECENT_WINDOW_SEC),
            "socket_errors_total": len(errors),
            "socket_reconnects_3min": sum(1 for x in reconnects if now - x <= RECENT_WINDOW_SEC),
            "socket_reconnects_total": len(reconnects),
            "catchup_pending": self._source.catchup_pending_count(),
            "watch_jobs": self._source.watch_job_count(),
        }


class StateSnapshotWriter:
    """스냅샷을 파일에 원자적으로 갈아 끼운다.

    임시 파일에 먼저 쓴 뒤 `replace` 로 옮긴다 — 반쯤 쓰인 파일을 읽히지
    않게 하는 것이 계약의 일부다. 값 수집·쓰기 어느 쪽이 실패해도 예외를
    밖으로 내지 않는다. 이 기록은 관측용이라, 실패했다고 요청 처리를
    멈추면 안 되기 때문이다.
    """

    def __init__(self, path: Path, builder: StateSnapshotBuilder) -> None:
        self._path = path
        self._builder = builder

    def write(self) -> None:
        try:
            snapshot = self._builder.build()
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)
        except Exception:
            log.exception("상태 스냅샷 기록 실패")
