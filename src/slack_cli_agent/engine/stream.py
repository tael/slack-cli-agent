"""Runs an engine subprocess while handing each stdout line to a callback.

Used when the engine writes its own structured progress events to stdout
(Engine.streams_progress) instead of through a hook process. The full
stdout is still collected and returned, so Engine.parse() sees exactly
what subprocess.run() would have given it.

Both pipes are drained by reader threads. Doing stdout in this thread and
stderr nowhere would deadlock as soon as an engine fills the stderr pipe
buffer, and waiting on the process before draining would deadlock on
stdout -- the same reason subprocess.run() uses communicate().
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable, Mapping
from typing import IO

#: How long to wait for the reader threads after the process exits. They
#: end when their pipes close, which the exit already did; this only keeps
#: a stuck thread from holding the request.
_DRAIN_TIMEOUT_SEC = 5.0


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
    on_stdout_line: Callable[[str], None],
) -> subprocess.CompletedProcess[str]:
    """subprocess.run() with a per-line callback on stdout. Raises TimeoutExpired like it does."""
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
    )
    out_chunks: list[str] = []
    err_chunks: list[str] = []
    readers = [
        threading.Thread(target=_drain, args=(process.stdout, out_chunks, on_stdout_line), daemon=True),
        threading.Thread(target=_drain, args=(process.stderr, err_chunks, None), daemon=True),
    ]
    for reader in readers:
        reader.start()
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as expired:
        process.kill()
        process.wait()
        # subprocess.run() 은 중단될 때 그때까지 받은 출력을 예외에 실어 준다.
        # Popen 경로는 직접 실어야 한다 - 안 그러면 무엇을 하다 멈췄는지가
        # 사라진다 (B11). 죽인 뒤 reader 를 잠깐 join 해 파이프에 남은 꼬리까지
        # 거둔다.
        for reader in readers:
            reader.join(_DRAIN_TIMEOUT_SEC)
        # text=True 라 실제론 str 이지만 typeshed 는 bytes 로 본다.
        expired.stdout = "".join(out_chunks)  # type: ignore[assignment]
        expired.stderr = "".join(err_chunks)  # type: ignore[assignment]
        raise
    for reader in readers:
        reader.join(_DRAIN_TIMEOUT_SEC)
    return subprocess.CompletedProcess(cmd, returncode, "".join(out_chunks), "".join(err_chunks))
