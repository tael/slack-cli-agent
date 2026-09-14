"""워커 프로세스가 SIGKILL 로 죽었을 때 그 작업이 다시 처리되는지 검증한다.

단위 시험(`tests/unit/test_jobs.py`)은 전부 대역이나 같은 프로세스 안의 스레드로
동시성을 흉내낸다. 여기서는 실제 자식 프로세스를 `subprocess` 로 띄우고 실제
SQLite 파일을 공유하게 한 뒤, 그 프로세스를 `SIGKILL` 로 강제 종료해 크래시를
재현한다. `terminate()`(SIGTERM) 를 쓰지 않는 이유는 정리 코드가 돌 여지를 주면
크래시가 아니라 정상 종료가 되기 때문이다.

느리므로 `@pytest.mark.integration` 을 붙인다. 기본 실행에서 제외하지는 않는다.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.context import RequestContext
from slack_cli_agent.jobs.heartbeat import WorkerHeartbeat
from slack_cli_agent.jobs.ports import JobStatus
from slack_cli_agent.jobs.queue import SqliteJobQueue
from slack_cli_agent.storage.database import Database

pytestmark = pytest.mark.integration

_SCRIPT = Path(__file__).with_name("_worker_script.py")
_CLAIM_TIMEOUT_SEC = 10.0
_PROC_EXIT_TIMEOUT_SEC = 10.0


def _ctx(ts: str, channel: str = "C_TEST") -> RequestContext:
    return RequestContext(channel=channel, user="U_TEST", ts=ts, thread_ts=ts, text="본문")


def _spawn_worker(action: str, db_path: Path, worker_id: str, status_path: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(_SCRIPT), action, str(db_path), worker_id, str(status_path)],
    )


def _wait_for_status(status_path: Path, timeout: float = _CLAIM_TIMEOUT_SEC) -> dict:
    """자식이 상태 파일을 쓸 때까지 짧게 폴링한다. 마감을 넘기면 명확히 실패시킨다."""
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if status_path.exists():
            try:
                return json.loads(status_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                last_error = exc
        time.sleep(0.02)
    raise AssertionError(
        f"{timeout}초 안에 상태 파일이 생기지 않았다: {status_path} (마지막 오류: {last_error})"
    )


def _kill_and_wait(proc: subprocess.Popen, timeout: float = _PROC_EXIT_TIMEOUT_SEC) -> None:
    """SIGKILL 을 보내고 프로세스가 실제로 죽을 때까지 기다린다."""
    with suppress(ProcessLookupError):
        os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=timeout)


def _cleanup(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        with suppress(ProcessLookupError):
            os.kill(proc.pid, signal.SIGKILL)
        with suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=_PROC_EXIT_TIMEOUT_SEC)


class TestCrashedWorkerJobIsReclaimed:
    """워커를 SIGKILL 로 죽인 뒤 그 작업이 회수돼 다시 처리되는지 검증한다."""

    def test_죽은_워커의_작업이_RUNNING으로_남고_회수_후_다시_집힌다(self, tmp_path: Path) -> None:
        db_path = tmp_path / "state.db"
        database = Database(db_path)
        database.migrate()
        queue = SqliteJobQueue(database)
        queue.enqueue(_ctx("1.1"))

        status_path = tmp_path / "held.json"
        proc = _spawn_worker("claim_and_hold", db_path, "죽는워커", status_path)
        try:
            status = _wait_for_status(status_path)
            assert status["job_id"] == 1

            # 자식이 작업을 집었으니 DB 에는 RUNNING 상태로 남아 있어야 한다.
            assert queue.counts() == {JobStatus.RUNNING.value: 1}

            _kill_and_wait(proc)

            # 죽은 뒤에도 아무도 상태를 되돌리지 않았으므로 여전히 RUNNING 이다.
            # 이것이 "워커 크래시가 작업을 영구히 실행 중 상태로 남긴다" 는
            # 문제 상황 자체다.
            assert queue.counts() == {JobStatus.RUNNING.value: 1}

            settings = RuntimeSettings(heartbeat_stale_sec=0, job_max_attempts=3)
            heartbeat = WorkerHeartbeat(queue, settings)
            result = heartbeat.reclaim_stale()

            assert [c.ts for c in result.requeued] == ["1.1"]
            assert queue.counts() == {JobStatus.QUEUED.value: 1}

            second = queue.claim_next("산워커")
            assert second is not None
            assert second.context.ts == "1.1"
            # 첫 집기에서 1, 회수 후 다시 집어 2가 된다.
            assert second.attempts == 2
        finally:
            _cleanup(proc)

    def test_재시도_상한을_넘으면_더_이상_회수되지_않는다(self, tmp_path: Path) -> None:
        db_path = tmp_path / "state.db"
        database = Database(db_path)
        database.migrate()
        queue = SqliteJobQueue(database)
        queue.enqueue(_ctx("2.1"))

        status_path = tmp_path / "held.json"
        proc = _spawn_worker("claim_and_hold", db_path, "죽는워커", status_path)
        try:
            status = _wait_for_status(status_path)
            assert status["job_id"] is not None

            _kill_and_wait(proc)

            settings = RuntimeSettings(heartbeat_stale_sec=0, job_max_attempts=1)
            heartbeat = WorkerHeartbeat(queue, settings)
            result = heartbeat.reclaim_stale()

            # 이미 시도 1회(claim_and_hold 의 claim_next) 로 상한 1에 도달했으니
            # 대기로 되돌리지 않고 바로 실패로 확정한다.
            assert result.requeued == []
            assert [c.ts for c in result.failed] == ["2.1"]
            assert queue.counts() == {JobStatus.FAILED.value: 1}

            # 실패로 확정됐으므로 더는 아무 워커도 이 작업을 집을 수 없다.
            assert queue.claim_next("또다른워커") is None

            # 재차 회수를 불러도 이미 FAILED 라 아무 것도 더 처리되지 않는다.
            again = heartbeat.reclaim_stale()
            assert again.total == 0
        finally:
            _cleanup(proc)


class TestConcurrentClaimAcrossProcesses:
    """두 프로세스가 동시에 같은 작업을 집으려 해도 하나만 성공하는지 검증한다."""

    def test_두_프로세스가_동시에_불러도_한_프로세스만_집는다(self, tmp_path: Path) -> None:
        db_path = tmp_path / "state.db"
        database = Database(db_path)
        database.migrate()
        queue = SqliteJobQueue(database)
        queue.enqueue(_ctx("3.1"))

        status_a = tmp_path / "status_a.json"
        status_b = tmp_path / "status_b.json"
        proc_a = _spawn_worker("claim_once", db_path, "워커A", status_a)
        proc_b = _spawn_worker("claim_once", db_path, "워커B", status_b)
        try:
            proc_a.wait(timeout=_PROC_EXIT_TIMEOUT_SEC)
            proc_b.wait(timeout=_PROC_EXIT_TIMEOUT_SEC)
            assert proc_a.returncode == 0
            assert proc_b.returncode == 0

            result_a = _wait_for_status(status_a, timeout=1.0)
            result_b = _wait_for_status(status_b, timeout=1.0)

            job_ids = {result_a["job_id"], result_b["job_id"]}
            # 둘 중 정확히 하나만 작업을 집는다: {1, None} 이어야 하고
            # {1, 1}(중복 클레임)이나 {None, None}(둘 다 실패)이면 안 된다.
            assert job_ids == {1, None}

            # 실제로 집힌 것은 한 건뿐이라 DB 에도 RUNNING 이 하나만 있어야 한다.
            assert queue.counts() == {JobStatus.RUNNING.value: 1}

            claimed_job = queue.claim_next("세번째워커")
            assert claimed_job is None  # 이미 그 스레드에 RUNNING 작업이 있어 더 못 나온다
        finally:
            _cleanup(proc_a)
            _cleanup(proc_b)
