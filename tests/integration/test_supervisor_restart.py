"""재기동 감독 통합 (H2 검증 기준).

apply-request 를 쓴 뒤 60초 안에 PID 교체와 DM 이 일어나야 한다. 실제
프로세스를 띄우고 가짜 kickstart 가 옛 프로세스를 죽이고 새 프로세스를
띄워 PID 파일을 바꾼다. launchctl 만 가리고 시계, 파일, 프로세스는 진짜다.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from slack_cli_agent.reliability.restart import ApplyRequestWriter
from slack_cli_agent.reliability.supervisor import ApplySupervisor, Service

_CHILD = (
    "import os, sys, time, pathlib; "
    "pathlib.Path(sys.argv[1]).write_text(f'{os.getpid()}\\n'); "
    "time.sleep(120)"
)


def _spawn(pid_path: Path) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen([sys.executable, "-c", _CHILD, str(pid_path)], stdin=subprocess.DEVNULL)
    for _ in range(100):
        if pid_path.exists() and pid_path.read_text().strip() == str(proc.pid):
            return proc
        time.sleep(0.05)
    raise AssertionError("자식이 PID 를 쓰지 않았다")


def _alive(pid: str) -> bool:
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError):
        return False
    return True


def test_표식_뒤_60초_안에_PID_교체와_DM(tmp_path: Path) -> None:
    roles = {"local.t.worker": tmp_path / "worker.pid", "local.t.ingress": tmp_path / "ingress.pid"}
    procs = {label: _spawn(path) for label, path in roles.items()}
    before = {label: path.read_text().strip() for label, path in roles.items()}
    dm: list[str] = []

    def kickstart(label: str) -> bool:
        procs[label].terminate()
        procs[label].wait(timeout=10)
        procs[label] = _spawn(roles[label])
        return True

    sup = ApplySupervisor(
        request_path=tmp_path / "apply-request",
        running_path=tmp_path / "apply-running",
        services=[Service(label, path) for label, path in roles.items()],
        kickstart=kickstart,
        alive=_alive,
        notify=dm.append,
        grace_sec=0.2,
        confirm_timeout_sec=60,
        poll_sec=0.1,
    )
    try:
        start = time.monotonic()
        ApplyRequestWriter(path=tmp_path / "apply-request").write("C1:1.2")
        outcome = None
        while outcome is None and time.monotonic() - start < 60:
            outcome = sup.tick()
            time.sleep(0.1)
        elapsed = time.monotonic() - start
        assert outcome == "restarted"
        assert elapsed < 60
        for label, path in roles.items():
            after = path.read_text().strip()
            assert after != before[label] and _alive(after)
        assert len(dm) == 1 and "재기동 완료" in dm[0]
    finally:
        for proc in procs.values():
            proc.kill()
            proc.wait(timeout=10)
