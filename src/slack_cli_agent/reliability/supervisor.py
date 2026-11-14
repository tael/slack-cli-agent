"""Supervisor that carries out a turn's apply request (H2).

A turn can't restart its own bot (reliability/restart.py), so it leaves an
apply-request marker. This loop runs as its own launchd job, outside any
turn's sandbox: it claims the marker by renaming it to apply-running,
runs the pre-restart check if one is configured (H3, the py_compile and
regression run mametchi apply.sh did inside the turn), kickstarts every
service, and judges success by PID replacement. When nothing is left
serving it runs the rollback step, if one is configured, and starts the
services again. The outcome goes to the owner's DM.

It also writes the request itself when the install checkout moves on main,
since not every bot sharing an editable install restarts after an update.
That request waits until no job is running, so an update doesn't cut a turn
short; past a ceiling it is written anyway, so a busy bot still updates.

Until now mametchi watchdog.py take_over_sca_apply did this for the bot from
another repository, so stopping mametchi would have taken it away.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
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
#: Lines of a failed check's output carried into the DM.
CHECK_TAIL_LINES = 15
#: An ingress restart also ends that process's review threads. A "진행" row
#: newer than this defers the restart (watchdog.py SCA_REVIEW_ACTIVE_SEC).
REVIEW_ACTIVE_SEC = 1800.0
#: How often the install checkout's HEAD is read again.
INSTALL_CHECK_SEC = 60.0
GIT_TIMEOUT_SEC = 5.0
#: How long an install update waits for the worker to have no running job
#: before it asks for the restart anyway. RuntimeSettings.restart_idle_max_wait_sec.
RESTART_IDLE_MAX_WAIT_SEC = 1800.0

GitRunner = Callable[[list[str]], str | None]


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


def running_jobs(db_path: Path, *, now: float, stale_sec: float) -> int:
    """Counts jobs a worker is handling right now. Read-only.

    A RUNNING row whose heartbeat stopped belongs to a dead worker and is
    left to stale reclaim, so it does not hold an update back. A missing or
    unreadable database counts as none, the same as active_reviews.
    """
    if not db_path.exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status = 'RUNNING'"
                " AND COALESCE(heartbeat_ts, started_at, 0) >= ?",
                (now - stale_sec,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row else 0


@dataclass
class _PendingUpdate:
    """An install update seen on main, waiting for an idle worker."""

    head: str
    since: float
    logged: bool = False


def git_in(directory: Path) -> GitRunner:
    """Runs git in `directory`; any failure (no git, not a checkout, hang) is None."""

    def run(args: list[str]) -> str | None:
        try:
            done = subprocess.run(
                ["git", "-C", str(directory), *args],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=GIT_TIMEOUT_SEC, check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    return run


def install_root(package_dir: Path) -> Path | None:
    """The git checkout the package is installed from, for an editable install.

    A wheel inside some other repository (a venv under a project) is not
    this package's checkout, so the package must sit at <root>/src/<name>.
    """
    top = git_in(package_dir)(["rev-parse", "--show-toplevel"])
    if not top:
        return None
    root = Path(top).resolve()
    if (root / "src" / package_dir.name).resolve() != package_dir.resolve():
        return None
    return root


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
        check: Callable[[], tuple[bool, str]] | None = None,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        grace_sec: float = APPLY_REQUEST_GRACE_SEC,
        run_ttl_sec: float = APPLY_RUN_TTL_SEC,
        confirm_timeout_sec: float = CONFIRM_TIMEOUT_SEC,
        poll_sec: float = 1.0,
        install_dir: Path | None = None,
        auto_apply: bool = True,
        git: GitRunner | None = None,
        install_check_sec: float = INSTALL_CHECK_SEC,
        inflight: Callable[[], int] = lambda: 0,
        idle_max_wait_sec: float = RESTART_IDLE_MAX_WAIT_SEC,
    ) -> None:
        self._request = request_path
        self._running = running_path
        self._services = tuple(services)
        self._kickstart = kickstart
        self._alive = alive
        self._notify = notify
        self._busy = busy
        self._rollback = rollback
        self._check = check
        self._check_tail = ""
        self._now = now
        self._sleep = sleep
        self._grace = grace_sec
        self._ttl = run_ttl_sec
        self._timeout = confirm_timeout_sec
        self._poll = poll_sec
        self._git = None
        if auto_apply and install_dir is not None:
            self._git = git or git_in(install_dir)
        self._install_every = install_check_sec
        self._install_checked = self._now()
        self._baseline = self._git(["rev-parse", "HEAD"]) if self._git else None
        self._inflight = inflight
        self._idle_max_wait = idle_max_wait_sec
        self._update: _PendingUpdate | None = None

    def tick(self) -> str | None:
        """One pass. Returns the outcome, or None when there was nothing to do."""
        self._watch_install()
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
            outcome = "check_failed" if not self._checked() else self._apply()
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

    def _watch_install(self) -> None:
        """Requests a restart when the install checkout moved on main.

        Bots sharing one editable install don't all restart after an update;
        without this a worker keeps the code it booted with while hook
        scripts already run the new code. Off a main branch a turn may be
        editing the checkout, so nothing is requested and the baseline is
        kept until main is back.

        The request waits for a moment with no running job. Writing it makes
        the worker stop claiming, and the kickstart's TERM kills a turn that
        outlasts the shutdown grace, which then reruns from scratch
        (2026-11-07..11-13: four turns lost 2,981s). HEAD is read every
        install_check_sec; the idle check runs on every tick.
        """
        if self._git is None:
            return
        if self._now() - self._install_checked >= self._install_every:
            self._install_checked = self._now()
            self._detect_install_change()
        if self._update is not None:
            self._request_when_idle()

    def _eligible_head(self) -> str | None:
        """HEAD when the checkout is on main with no tracked change, else None."""
        head = self._git(["rev-parse", "HEAD"]) if self._git else None
        if not head or self._git is None:
            return None
        if self._git(["rev-parse", "--abbrev-ref", "HEAD"]) != "main":
            return None
        if self._git(["status", "--porcelain", "--untracked-files=no"]) != "":
            return None
        return head

    def _markers_present(self) -> bool:
        return self._request.exists() or self._running.exists()

    def _detect_install_change(self) -> None:
        assert self._git is not None
        head = self._git(["rev-parse", "HEAD"])
        if not head:
            return
        if self._baseline is None:
            self._baseline = head
            return
        if head == self._baseline:
            if self._update is not None:
                log.info("설치본이 기동 때 HEAD 로 돌아와 갱신 재기동을 거둔다")
                self._update = None
            return
        if self._eligible_head() != head:
            # Off main or dirty: the same as before, nothing is requested
            # until main is back. The wait ceiling starts over from there.
            self._update = None
            return
        if self._markers_present():
            # That restart loads this HEAD too, unless it began before the
            # commit; a second restart for it would only drain the worker again.
            self._baseline = head
            self._update = None
            return
        if self._update is None:
            self._update = _PendingUpdate(head=head, since=self._now())
        else:
            self._update = _PendingUpdate(
                head=head, since=self._update.since, logged=self._update.logged
            )

    def _request_when_idle(self) -> None:
        update = self._update
        assert update is not None
        if self._markers_present():
            self._baseline = update.head
            self._update = None
            return
        try:
            running = self._inflight()
        except Exception:
            # Unknown counts as busy; the ceiling still bounds the wait.
            log.exception("진행 중 작업 수 조회 실패")
            running = 1
        waited = self._now() - update.since
        if running > 0 and waited < self._idle_max_wait:
            if not update.logged:
                log.info(
                    "진행 중 작업 %d건, 설치본 갱신 재기동을 유휴 때까지 미룬다 (최대 %.0f초)",
                    running, self._idle_max_wait,
                )
                update.logged = True
            return
        # Checked again here: the checkout may have left main since detection.
        head = self._eligible_head()
        if head is None or head == self._baseline:
            return
        assert self._baseline is not None
        note = f"설치본 갱신 {self._baseline[:7]} -> {head[:7]}"
        if running > 0:
            note += f" (진행 중 작업 {running}건, {waited:.0f}초 대기 상한 도달)"
        request = ApplyRequest(requested_at=self._now(), note=note)
        self._request.parent.mkdir(parents=True, exist_ok=True)
        staging = self._request.with_name(self._request.name + ".tmp")
        staging.write_text(request.render(), encoding="utf-8")
        staging.replace(self._request)
        log.info("설치본 갱신 재기동 요청 : %s (%.0f초 대기)", note, waited)
        self._baseline = head
        self._update = None

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
            try:
                kicked = self._kickstart(service.label)
            except Exception:
                # Raising here would skip _report and drop the request silently.
                log.exception("kickstart 예외 : %s", service.label)
                kicked = False
            if not kicked:
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

    def _checked(self) -> bool:
        """Runs the check before anything is restarted. A failure leaves the
        running bot as it is: the new code has not been loaded yet."""
        self._check_tail = ""
        if self._check is None:
            return True
        try:
            ok, output = self._check()
        except Exception as exc:
            # Raising here would skip _report and drop the request silently.
            log.exception("재기동 전 점검 예외")
            ok, output = False, f"점검을 실행하지 못했습니다 : {exc}"
        if not ok:
            self._check_tail = "\n".join(output.strip().splitlines()[-CHECK_TAIL_LINES:])
        return ok

    def _apply(self) -> str:
        if self._restart():
            return "restarted"
        if not should_rollback(restarted=False, process_running=self._running_any()):
            return "failed_running"
        if self._rollback is None:
            return "failed_down"
        try:
            rolled = self._rollback()
        except Exception:
            # Same as kickstart: raising here would skip _report while the bot is down.
            log.exception("rollback 예외")
            rolled = False
        if not rolled:
            return "rollback_failed"
        return "rolled_back" if self._restart() else "rollback_failed"

    def _report(self, outcome: str, request: ApplyRequest) -> None:
        stamp = time.strftime("%m-%d %H:%M:%S", time.localtime(self._now()))
        head = {
            "check_failed": "점검 실패, 재기동하지 않았습니다",
            "restarted": "재기동 완료",
            "failed_running": "재기동 실패, 되돌리지 않았습니다",
            "failed_down": "재기동 실패, 되돌릴 수단이 없습니다",
            "rolled_back": "재기동 실패, 되돌렸습니다",
            "rollback_failed": "재기동 실패, 되돌려도 기동하지 못했습니다",
        }[outcome]
        detail = {
            "check_failed": "기존 프로세스가 그대로 돌고 있습니다. 점검 출력 끝부분을 붙입니다",
            "restarted": "새 PID 로 교체를 확인했습니다",
            "failed_running": "PID 가 바뀌지 않았고 기존 프로세스가 계속 돌고 있습니다",
            "failed_down": "돌고 있는 프로세스가 없습니다. 직접 확인이 필요합니다",
            "rolled_back": "되돌린 코드로 기동을 확인했습니다",
            "rollback_failed": "돌고 있는 프로세스가 없습니다. 직접 확인이 필요합니다",
        }[outcome]
        lines = [f"*{head}*", f"- 시각 : {stamp}", f"- 상태 : {detail}"]
        if request.turn_id:
            lines.append(f"- 요청 턴 : {request.turn_id}")
        if request.note:
            first, _, rest = request.note.partition("\n")
            lines.append(f"- 변경 : {first}")
            if rest.strip():
                lines.append(rest.strip())
        if outcome == "check_failed" and self._check_tail:
            lines.append(f"```\n{self._check_tail}\n```")
        try:
            self._notify("\n".join(lines))
        except Exception:
            log.exception("재기동 결과 알림 실패 : %s", outcome)
        log.info("재기동 결과 : %s", outcome)
