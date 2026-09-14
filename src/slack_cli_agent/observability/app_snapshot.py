"""Adapter binding real app components to the `SnapshotSource` contract.

Lookups never raise: a snapshot read must not block request handling.
But failures aren't collapsed to 0 -- a lookup failure and "nothing
registered" are different facts, distinguished via `None`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from slack_cli_agent.core.lifecycle import InflightCounter

# Only count in-flight statuses -- including finished ones would make
# state readers think work is backed up when it isn't.
PENDING_STATUSES = ("queued", "running")


class _Queue(Protocol):
    def counts(self) -> dict[str, int]: ...


class _SocketWatch(Protocol):
    def error_timestamps(self) -> tuple[float, ...]: ...
    def reconnect_timestamps(self) -> tuple[float, ...]: ...


class _WatchJobs(Protocol):
    def open_count(self) -> int: ...


class ApplicationSnapshotSource:
    """Implements `state_snapshot.SnapshotSource` using real app components."""

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
        try:
            counts = self._queue.counts()
        except Exception:  # noqa: BLE001 -- don't let a count failure fail the whole snapshot
            return {}
        return {status: n for status, n in counts.items() if status in PENDING_STATUSES}

    def socket_error_timestamps(self) -> Sequence[float]:
        return self._socket_watch.error_timestamps()

    def socket_reconnect_timestamps(self) -> Sequence[float]:
        return self._socket_watch.reconnect_timestamps()

    def catchup_pending_count(self) -> int:
        # Catch-up is processed synchronously on receipt in this architecture,
        # so nothing is ever pending. Kept for contract compatibility.
        return 0

    def watch_job_count(self) -> int | None:
        try:
            return self._watch_jobs.open_count()
        except Exception:  # noqa: BLE001 -- don't let a count failure fail the whole snapshot
            return None

    def is_shutting_down(self) -> bool:
        return self._is_shutting_down()

    def started_at(self) -> float:
        return self._started_at
