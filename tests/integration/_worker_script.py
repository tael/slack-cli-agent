"""통합 시험 전용 자식 프로세스 스크립트.

`subprocess` 로 기동되는 완전히 별도의 파이썬 프로세스다. 부모 프로세스와는
객체를 주고받지 않는다 - DB 파일 경로만 인자로 받아 그 안에서 큐를 새로
만들고, 결과는 상태 파일(JSON)에 적어 부모가 폴링으로 읽게 한다.

pytest 의 홈 격리(conftest.py 의 sys.addaudithook)는 이 프로세스에 적용되지
않는다 - 별도 인터프리터라 그 훅이 안 걸린다. 그래서 이 스크립트는 conftest 를
import 하지 않고 독립적으로 동작한다.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# 이 파일은 <repo>/tests/integration/_worker_script.py 에 있다.
# parents[2] 가 저장소 루트이고 그 아래 src 에 패키지가 있다.
_SRC = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(_SRC))

from slack_cli_agent.jobs.queue import SqliteJobQueue  # noqa: E402
from slack_cli_agent.storage.database import Database  # noqa: E402


def _make_queue(db_path: str) -> SqliteJobQueue:
    database = Database(Path(db_path))
    database.migrate()
    return SqliteJobQueue(database)


def _write_status(status_path: str, payload: dict) -> None:
    Path(status_path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def claim_and_hold(db_path: str, worker_id: str, status_path: str) -> None:
    """작업을 하나 집고, 상태를 기록한 뒤 갱신 없이 그대로 멈춘다.

    heartbeat 갱신 스레드를 안 돌린다. 크래시난 워커를 흉내내는 것이므로
    죽을 때까지(부모가 SIGKILL 할 때까지) 프로세스가 살아 있어야 한다.
    """
    queue = _make_queue(db_path)
    job = queue.claim_next(worker_id)
    _write_status(
        status_path,
        {"job_id": job.id if job is not None else None, "pid": os.getpid()},
    )
    while True:
        time.sleep(3600)


def claim_once(db_path: str, worker_id: str, status_path: str) -> None:
    """작업을 한 번 집어 보고 즉시 종료한다. 동시 클레임 시험에 쓴다."""
    queue = _make_queue(db_path)
    job = queue.claim_next(worker_id)
    _write_status(
        status_path,
        {"job_id": job.id if job is not None else None, "pid": os.getpid()},
    )


_ACTIONS = {
    "claim_and_hold": claim_and_hold,
    "claim_once": claim_once,
}


if __name__ == "__main__":
    action_name, *action_args = sys.argv[1:]
    action = _ACTIONS.get(action_name)
    if action is None:
        raise SystemExit(f"알 수 없는 동작: {action_name}")
    action(*action_args)
