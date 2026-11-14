"""Runs an engine subprocess, optionally handing each stdout line to a callback.

Every engine run goes through here, with or without the callback. The
callback is for engines that write their own progress events to stdout
(Engine.streams_progress); the deadline below is why the runs without one
come here too. subprocess.run() enforces its limit through
Popen.wait(timeout=), which is exactly the call that did not come back on
10-16, and it leaves the engine's own tool subprocesses untouched. The
full stdout is still collected and returned, so Engine.parse() sees
exactly what subprocess.run() would have given it.

Both pipes are drained by reader threads. Doing stdout in this thread and
stderr nowhere would deadlock as soon as an engine fills the stderr pipe
buffer, and waiting on the process before draining would deadlock on
stdout -- the same reason subprocess.run() uses communicate().
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
from collections.abc import Callable, Mapping
from typing import IO

log = logging.getLogger(__name__)

#: How long to wait for the reader threads after the process exits. They
#: end when their pipes close, which the exit already did; this only keeps
#: a stuck thread from holding the request.
_DRAIN_TIMEOUT_SEC = 5.0


def _kill_group(process: subprocess.Popen[str]) -> None:
    """SIGKILL the whole session the engine was started in.

    2026-10-16: an agy turn ran 27,558s against a 900s limit and
    Popen.wait(timeout=) never came back, so the engine was never killed.
    Each stale-job reclaim then started another one against the same
    request while the earlier ones stayed alive, and all of them kept
    writing the same progress log -- the thread showed an endless run of
    "답 쓰는 중 / 명령 실행 중" (sca-rei-spin).

    A deadline timer owned by this module does not depend on wait()
    waking up, and killing the group rather than the child reaches the
    tool subprocesses the engine spawned.
    """
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # Already gone, or not ours to signal. Fall back to the child alone
        # so a group that cannot be reached still loses its leader.
        try:
            process.kill()
        except ProcessLookupError:
            return


def _drain(stream: IO[str], chunks: list[str], on_line: Callable[[str], None] | None) -> None:
    with stream:
        for line in stream:
            chunks.append(line)
            if on_line is None:
                continue
            try:
                on_line(line)
            except Exception:  # noqa: BLE001 - progress display must not change the answer
                continue


def run_streaming(
    cmd: list[str],
    cwd: str,
    timeout: float,
    env: Mapping[str, str] | None,
    on_stdout_line: Callable[[str], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """subprocess.run() with an optional per-line callback on stdout.

    Raises TimeoutExpired like subprocess.run() does, but on a timer of its
    own rather than on wait(timeout=).
    """
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        # Engine CLIs read stdin before the prompt argument; an inherited open
        # stdin never gives EOF and the call hangs until timeout (H67).
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(env) if env is not None else None,
        # Puts the engine and everything it spawns in their own group, so the
        # deadline below can reach the tool subprocesses too.
        start_new_session=True,
    )
    out_chunks: list[str] = []
    err_chunks: list[str] = []
    readers = [
        threading.Thread(target=_drain, args=(process.stdout, out_chunks, on_stdout_line), daemon=True),
        threading.Thread(target=_drain, args=(process.stderr, err_chunks, None), daemon=True),
    ]
    for reader in readers:
        reader.start()
    expired = threading.Event()

    def _on_deadline() -> None:
        expired.set()
        log.warning("제한 %.0f초를 넘겨 엔진 프로세스 그룹을 강제 종료한다 : pid=%d", timeout, process.pid)
        _kill_group(process)

    # The enforcer, not a backstop: wait(timeout=) has been seen not to
    # return at all, and this timer is what actually ends the turn.
    deadline = threading.Timer(timeout, _on_deadline)
    deadline.daemon = True
    deadline.start()
    try:
        returncode = process.wait()
    finally:
        deadline.cancel()
    if expired.is_set():
        # Reported the same way subprocess.run() would, so the runner's
        # existing timeout branch produces the reply and the audit reason.
        for reader in readers:
            reader.join(_DRAIN_TIMEOUT_SEC)
        raise subprocess.TimeoutExpired(
            cmd, timeout, output="".join(out_chunks), stderr="".join(err_chunks)
        )
    for reader in readers:
        reader.join(_DRAIN_TIMEOUT_SEC)
    return subprocess.CompletedProcess(cmd, returncode, "".join(out_chunks), "".join(err_chunks))
