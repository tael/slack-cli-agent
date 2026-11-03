"""Apply-delegated restart: request handoff, PID-replacement judgment, rollback.

A bot turn runs inside that turn's sandbox, and `launchctl kickstart` is
rejected from inside it ("Operation not permitted" — measured against
~/Projects/mametchi-slack-bot on 2026-09-22 23:01). Calling the restart
directly from a turn also deadlocks: the restart waits for the turn's
in-flight request to finish, and that request is the one that triggered the
restart. So a turn never restarts itself; it only writes a request marker
for a supervisor process — started by launchd, outside any turn's sandbox —
to pick up and act on later.

PID replacement, not liveness, is the restart's success signal. A quiet
no-op kickstart leaves the old process (and its PID file) untouched, which
looks identical to "still healthy" if only liveness is checked.
~/Projects/mametchi-slack-bot's apply-restart.sh hit exactly this on
2026-09-22 22:02: 10-hour-old code kept running and was reported as a
completed restart.

Rollback is gated on whether *anything* is still serving, not on whether
the new PID came up. Restoring the previous copy while a process is still
running throws away a build that was never proven bad — plenty of restart
failures have had nothing to do with the new code (sandboxed kickstart,
missing PATH for a dependency lookup).
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ApplyRequest:
    requested_at: float
    turn_id: str | None = None

    def render(self) -> str:
        # First line is the timestamp pending_apply ages against; the turn id,
        # if present, lets the bot process skip waiting on this one request
        # when it later sees a shutdown signal (avoids the same deadlock the
        # marker exists to dodge).
        lines = [str(int(self.requested_at))]
        if self.turn_id:
            lines.append(self.turn_id)
        return "\n".join(lines) + "\n"


class ApplyRequestWriter:
    """Writes the apply-request marker an external supervisor polls for."""

    def __init__(self, *, path: Path, now: Callable[[], float] = time.time) -> None:
        self._path = path
        self._now = now

    def write(self, turn_id: str | None = None) -> ApplyRequest:
        request = ApplyRequest(requested_at=self._now(), turn_id=turn_id)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(request.render(), encoding="utf-8")
        return request


class RestartLock:
    """Atomic mkdir lock so two applies can't restart the bot at once.

    mkdir either creates the directory or fails — unlike a plain existence
    check, two concurrent callers can't both read "free" and both proceed.
    A lock left behind by a run that was killed mid-restart would otherwise
    jam every later apply forever, so one older than stale_after_sec is
    treated as abandoned and cleared before the retry.
    """

    def __init__(
        self,
        *,
        path: Path,
        stale_after_sec: float = 1200.0,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._stale_after_sec = stale_after_sec
        self._now = now

    def acquire(self) -> bool:
        if self._try_mkdir():
            return True
        try:
            age = self._now() - self._path.stat().st_mtime
        except OSError:
            # Vanished between the failed mkdir and this stat; try once more.
            return self._try_mkdir()
        if age < self._stale_after_sec:
            return False
        shutil.rmtree(self._path, ignore_errors=True)
        return self._try_mkdir()

    def release(self) -> None:
        shutil.rmtree(self._path, ignore_errors=True)

    def _try_mkdir(self) -> bool:
        try:
            self._path.mkdir(parents=True)
        except FileExistsError:
            return False
        # Stamp with the injected clock rather than trusting the filesystem's
        # own wall-clock mtime, so staleness checks are reproducible under a
        # fake clock in tests and still correct in production (now defaults
        # to time.time).
        stamp = self._now()
        os.utime(self._path, (stamp, stamp))
        return True


def restart_confirmed(
    *,
    pid_before: str | None,
    pid_after: str | None,
    alive: Callable[[str], bool],
) -> bool:
    """True only if a *new* process replaced the old one and is alive.

    `pid_after` equal to `pid_before` (or empty) means the restart never
    took effect, regardless of whether the old process happens to still be
    running.
    """
    if not pid_after:
        return False
    if pid_before and pid_after == pid_before:
        return False
    return alive(pid_after)


def should_rollback(*, restarted: bool, process_running: bool) -> bool:
    """Decide whether to restore the pre-apply copy.

    Only when the restart did not land *and* nothing is left serving at
    all — a process still running on the old PID means the bot hasn't
    actually been lost, so there's nothing to roll back from yet.
    """
    if restarted:
        return False
    return not process_running
