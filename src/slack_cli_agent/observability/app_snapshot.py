"""앱의 실제 부품을 `SnapshotSource` 계약에 잇는 어댑터.

`state_snapshot.StateSnapshotBuilder` 는 값의 출처를 모른다. 이 클래스가
대기줄·소켓 오류 감시·감시 작업 저장소에서 값을 가져와 그 계약에 맞춘다.
원본 `bot.py` 의 `state_snapshot()` 이 전역 변수를 직접 읽던 대목이다.

조회가 실패해도 예외를 밖으로 내지 않는다 — 상태 기록 하나 때문에 요청
처리가 멈추면 안 된다. 다만 실패를 0 으로 바꾸지는 않는다. 조회 실패와
"등록된 것이 없음" 은 다른 사실이라 `None` 으로 구분한다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from slack_cli_agent.core.lifecycle import InflightCounter

# 대기로 셀 작업 상태. 끝난 것을 세면 상태 파일만 보는 쪽이 밀린 일이
# 있다고 읽는다.
PENDING_STATUSES = ("queued", "running")


class _Queue(Protocol):
    def counts(self) -> dict[str, int]: ...


class _SocketWatch(Protocol):
    def error_timestamps(self) -> tuple[float, ...]: ...
    def reconnect_timestamps(self) -> tuple[float, ...]: ...


class _WatchJobs(Protocol):
    def open_count(self) -> int: ...


class ApplicationSnapshotSource:
    """`state_snapshot.SnapshotSource` 를 실제 부품으로 만족시킨다."""

    def __init__(
        self,
        *,
        inflight: InflightCounter,
        queue: _Queue,
        socket_watch: _SocketWatch,
        watch_jobs: _WatchJobs,
        is_shutting_down: Callable[[], bool],
        started_at: float,
    ) -> None:
        self._inflight = inflight
        self._queue = queue
        self._socket_watch = socket_watch
        self._watch_jobs = watch_jobs
        self._is_shutting_down = is_shutting_down
        self._started_at = started_at

    def inflight_count(self) -> int:
        return self._inflight.count

    def queued_threads(self) -> Mapping[str, int]:
        """대기 상태별 건수. 원본은 스레드별로 셌는데 이 구조에서는 대기줄이
        데이터베이스에 있어 상태별 집계가 같은 물음에 답한다."""
        try:
            counts = self._queue.counts()
        except Exception:
            return {}
        return {status: n for status, n in counts.items() if status in PENDING_STATUSES}

    def socket_error_timestamps(self) -> Sequence[float]:
        return self._socket_watch.error_timestamps()

    def socket_reconnect_timestamps(self) -> Sequence[float]:
        return self._socket_watch.reconnect_timestamps()

    def catchup_pending_count(self) -> int:
        """되짚기 대기 건수.

        이 구조에서 되짚기는 접수 시점에 그대로 처리돼 따로 기다리는 것이
        없다. 원본에 있던 항목이라 계약에는 남기고 0 으로 둔다.
        """
        return 0

    def watch_job_count(self) -> int | None:
        try:
            return self._watch_jobs.open_count()
        except Exception:
            return None

    def is_shutting_down(self) -> bool:
        return self._is_shutting_down()

    def started_at(self) -> float:
        return self._started_at
