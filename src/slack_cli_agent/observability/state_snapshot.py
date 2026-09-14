# `StateSnapshotWriter.write()` never raises -- a failed snapshot must not
# affect request handling. Periodic execution is the caller's job, via
# `core.periodic.PeriodicRunner`.

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

RECENT_WINDOW_SEC = 180.0


class SnapshotSource(Protocol):
    """Supplies the raw values a snapshot is built from."""

    def inflight_count(self) -> int: ...

    def queued_threads(self) -> Mapping[str, int]: ...

    def socket_error_timestamps(self) -> Sequence[float]: ...

    def socket_reconnect_timestamps(self) -> Sequence[float]: ...

    def catchup_pending_count(self) -> int: ...

    def watch_job_count(self) -> int | None:
        """Number of registered watch jobs, or `None` if the lookup failed."""
        ...

    def is_shutting_down(self) -> bool: ...

    def started_at(self) -> float: ...


class StateSnapshotBuilder:

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
        # Drop threads with a zero count -- nothing is actually waiting there.
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
    """Writes the snapshot atomically (write to temp file, then replace) and never raises."""

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
