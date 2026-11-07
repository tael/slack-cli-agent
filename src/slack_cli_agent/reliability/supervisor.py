"""Supervisor that carries out a turn's apply request (H2).

A turn can't restart its own bot (reliability/restart.py), so it leaves an
apply-request marker. This loop runs as its own launchd job, outside any
turn's sandbox: it claims the marker by renaming it to apply-running,
kickstarts every service, and judges success by PID replacement. When
nothing is left serving it runs the rollback step, if one is configured,
and starts the services again. The outcome goes to the owner's DM.

Until now mametchi watchdog.py take_over_sca_apply did this for the bot from
another repository, so stopping mametchi would have taken it away.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .restart import ApplyRequest, restart_confirmed, should_rollback

log = logging.getLogger(__name__)

#: A marker this young may still be mid-write. Same as watchdog.py.
APPLY_REQUEST_GRACE_SEC = 15.0
#: An apply-running older than this was left by a supervisor that died
#: mid-restart, and is claimed again. Longer than the worker's 360s exit
#: timeout plus a rollback.
APPLY_RUN_TTL_SEC = 900.0
#: How long to wait for new PIDs. kickstart -k makes the worker drain in-flight
#: work (up to 330s) before it exits, so this exceeds launchd's ExitTimeOut.
CONFIRM_TIMEOUT_SEC = 400.0
#: An ingress restart also ends that process's review threads. A "진행" row
#: newer than this defers the restart (watchdog.py SCA_REVIEW_ACTIVE_SEC).
REVIEW_ACTIVE_SEC = 1800.0


@dataclass(frozen=True)
class Service:
    label: str
    pid_path: Path


def read_pid(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def active_reviews(db_path: Path, *, now: float, window_sec: float = REVIEW_ACTIVE_SEC) -> int:
    """Counts reviews still marked in progress. Read-only; a missing or
    unreadable database counts as none, so it never blocks a restart."""
    if not db_path.exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT result FROM reviews WHERE at >= ?", (now - window_sec,)
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return 0
    count = 0
    for (raw,) in rows:
        try:
            if json.loads(raw).get("status") == "진행":
                count += 1
        except (TypeError, ValueError, AttributeError):
            continue
    return count


class ApplySupervisor:
    def __init__(
        self,
        *,
        request_path: Path,
        running_path: Path,
        services: Sequence[Service],
        kickstart: Callable[[str], bool],
        alive: Callable[[str], bool],
        notify: Callable[[str], object],
        busy: Callable[[], int] = lambda: 0,
        rollback: Callable[[], bool] | None = None,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        grace_sec: float = APPLY_REQUEST_GRACE_SEC,
        run_ttl_sec: float = APPLY_RUN_TTL_SEC,
        confirm_timeout_sec: float = CONFIRM_TIMEOUT_SEC,
        poll_sec: float = 1.0,
    ) -> None:
        self._request = request_path
        self._running = running_path
        self._services = tuple(services)
        self._kickstart = kickstart
        self._alive = alive
        self._notify = notify
        self._busy = busy
        self._rollback = rollback
        self._now = now
        self._sleep = sleep
        self._grace = grace_sec
        self._ttl = run_ttl_sec
        self._timeout = confirm_timeout_sec
        self._poll = poll_sec

    def tick(self) -> str | None:
        """One pass. Returns the outcome, or None when there was nothing to do."""
        if not self._due():
            return None
        reviews = self._busy()
        if reviews:
            log.info("진행 중 점검 %d건. 재기동을 미룬다", reviews)
            return "deferred"
        request = self._claim()
        if request is None:
            return None
        try:
            outcome = self._apply()
        finally:
            # Only apply-running: a request written while this ran is the next
            # restart, and deleting it would drop it silently (apply-restart.sh).
            self._running.unlink(missing_ok=True)
        self._report(outcome, request)
        return outcome

    def run_forever(self, stopping: Callable[[], bool], interval_sec: float = 5.0) -> None:
        while not stopping():
            try:
                self.tick()
            except Exception:
                log.exception("재기동 감독 회차 실패")
            self._sleep(interval_sec)

    def _age(self, path: Path) -> float | None:
        try:
            return self._now() - path.stat().st_mtime
        except OSError:
            return None

    def _changed_age(self, path: Path) -> float | None:
        """Time since the file was last written, renamed or touched.

        A rename keeps st_mtime, so a request another supervisor (mametchi
        watchdog.py) has just moved into apply-running still carries the
        request's age, which after a long review deferral exceeds the TTL.
        The rename does update st_ctime, so the newer of the two is used.
        """
        try:
            st = path.stat()
        except OSError:
            return None
        return self._now() - max(st.st_mtime, st.st_ctime)

    def _abandoned(self) -> bool:
        age = self._changed_age(self._running)
        return age is not None and age > self._ttl

    def _due(self) -> bool:
        if self._running.exists():
            return self._abandoned()
        request = self._age(self._request)
        return request is not None and request >= self._grace

    def _claim(self) -> ApplyRequest | None:
        """Renames atomically so two passes can't run the same restart.

        apply-running can appear between _due and here (another supervisor
        took the request while _busy ran). It is taken over only when it is
        still abandoned at this point; otherwise it is someone else's restart.
        """
        if self._running.exists():
            if not self._abandoned():
                return None
        else:
            try:
                self._request.replace(self._running)
            except OSError:
                return None
        try:
            text = self._running.read_text(encoding="utf-8")
        except OSError:
            return None
        stamp = self._now()
        os.utime(self._running, (stamp, stamp))
        return ApplyRequest.parse(text) or ApplyRequest(requested_at=stamp)

    def _restart(self) -> bool:
        before = {s.label: read_pid(s.pid_path) for s in self._services}
        for service in self._services:
            if not self._kickstart(service.label):
                log.warning("kickstart 실패 : %s", service.label)
        deadline = self._now() + self._timeout
        while True:
            if all(
                restart_confirmed(
                    pid_before=before[s.label], pid_after=read_pid(s.pid_path), alive=self._alive
                )
                for s in self._services
            ):
                return True
            if self._now() >= deadline:
                return False
            self._sleep(self._poll)

    def _running_any(self) -> bool:
        return any(
            (pid := read_pid(s.pid_path)) is not None and self._alive(pid) for s in self._services
        )

    def _apply(self) -> str:
        if self._restart():
            return "restarted"
        if not should_rollback(restarted=False, process_running=self._running_any()):
            return "failed_running"
        if self._rollback is None:
            return "failed_down"
        if not self._rollback():
            return "rollback_failed"
        return "rolled_back" if self._restart() else "rollback_failed"

    def _report(self, outcome: str, request: ApplyRequest) -> None:
        stamp = time.strftime("%m-%d %H:%M:%S", time.localtime(self._now()))
        head = {
            "restarted": "재기동 완료",
            "failed_running": "재기동 실패, 되돌리지 않았습니다",
            "failed_down": "재기동 실패, 되돌릴 수단이 없습니다",
            "rolled_back": "재기동 실패, 되돌렸습니다",
            "rollback_failed": "재기동 실패, 되돌려도 기동하지 못했습니다",
        }[outcome]
        detail = {
            "restarted": "새 PID 로 교체를 확인했습니다",
            "failed_running": "PID 가 바뀌지 않았고 기존 프로세스가 계속 돌고 있습니다",
            "failed_down": "돌고 있는 프로세스가 없습니다. 직접 확인이 필요합니다",
            "rolled_back": "되돌린 코드로 기동을 확인했습니다",
            "rollback_failed": "돌고 있는 프로세스가 없습니다. 직접 확인이 필요합니다",
        }[outcome]
        lines = [f"*{head}*", f"- 시각 : {stamp}", f"- 상태 : {detail}"]
        if request.turn_id:
            lines.append(f"- 요청 턴 : {request.turn_id}")
        try:
            self._notify("\n".join(lines))
        except Exception:
            log.exception("재기동 결과 알림 실패 : %s", outcome)
        log.info("재기동 결과 : %s", outcome)
