"""설치본 코드가 바뀌면 감독이 스스로 재기동을 요청한다 (2026-11-12).

마메치는 데이전트와 같은 editable 설치본을 쓰지만 갱신 뒤 재기동하는 장치가
없어 워커가 옛 코드로 계속 돌았다. 감독이 설치본 HEAD 를 지켜보다가 main 에서
깨끗하게 바뀌었을 때만 apply-request 를 쓴다.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.reliability.restart import ApplyRequest
from slack_cli_agent.reliability.supervisor import ApplySupervisor, install_root

OLD = "a" * 40
NEW = "b" * 40


class 가짜git:
    def __init__(self) -> None:
        self.head: str | None = OLD
        self.branch = "main"
        self.dirty = ""

    def __call__(self, args: list[str]) -> str | None:
        if self.head is None:
            return None
        if args == ["rev-parse", "HEAD"]:
            return self.head
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return self.branch
        if args[0] == "status":
            return self.dirty
        raise AssertionError(args)


def 감독(tmp_path: Path, git: 가짜git, **kw: object) -> ApplySupervisor:
    args: dict[str, object] = {
        "request_path": tmp_path / "apply-request",
        "running_path": tmp_path / "apply-running",
        "services": [],
        "kickstart": lambda label: True,
        "alive": lambda pid: False,
        "notify": lambda text: None,
        "now": lambda: 5000.0,
        "install_dir": tmp_path,
        "git": git,
        "install_check_sec": 0,
    }
    args.update(kw)
    return ApplySupervisor(**args)  # type: ignore[arg-type]


def test_HEAD가_바뀌면_요청을_쓰고_기준을_갱신한다(tmp_path: Path) -> None:
    git = 가짜git()
    sup = 감독(tmp_path, git)
    sup.tick()
    assert not (tmp_path / "apply-request").exists()
    git.head = NEW
    sup.tick()
    request = ApplyRequest.parse((tmp_path / "apply-request").read_text())
    assert request is not None
    assert request.note == "설치본 갱신 aaaaaaa -> bbbbbbb"
    (tmp_path / "apply-request").unlink()
    sup.tick()
    assert not (tmp_path / "apply-request").exists()


def test_브랜치가_main이_아니면_쓰지_않고_기준도_두다(tmp_path: Path) -> None:
    git = 가짜git()
    sup = 감독(tmp_path, git)
    git.head, git.branch = NEW, "fix/x"
    sup.tick()
    assert not (tmp_path / "apply-request").exists()
    git.branch = "main"
    sup.tick()
    assert (tmp_path / "apply-request").exists()


def test_추적_파일이_더러우면_쓰지_않는다(tmp_path: Path) -> None:
    git = 가짜git()
    sup = 감독(tmp_path, git)
    git.head, git.dirty = NEW, " M src/x.py"
    sup.tick()
    assert not (tmp_path / "apply-request").exists()


def test_요청이나_실행이_이미_있으면_쓰지_않는다(tmp_path: Path) -> None:
    git = 가짜git()
    sup = 감독(tmp_path, git, now=lambda: 0.0)
    (tmp_path / "apply-running").write_text("0\nC1:1.2\n턴이 쓴 요청\n")
    git.head = NEW
    sup.tick()
    assert not (tmp_path / "apply-request").exists()
    assert "턴이 쓴 요청" in (tmp_path / "apply-running").read_text()

    (tmp_path / "apply-running").unlink()
    (tmp_path / "apply-request").write_text("0\nC1:1.2\n턴이 쓴 요청\n")
    git.head = "c" * 40
    sup.tick()
    assert "턴이 쓴 요청" in (tmp_path / "apply-request").read_text()


def test_설정이_꺼져_있으면_쓰지_않는다(tmp_path: Path) -> None:
    git = 가짜git()
    sup = 감독(tmp_path, git, auto_apply=False)
    git.head = NEW
    sup.tick()
    assert not (tmp_path / "apply-request").exists()
    assert RuntimeSettings().supervisor_auto_apply is True


def test_git이_없으면_조용히_넘긴다(tmp_path: Path) -> None:
    git = 가짜git()
    git.head = None
    sup = 감독(tmp_path, git)
    assert sup.tick() is None
    assert not (tmp_path / "apply-request").exists()


def test_점검_간격_안에서는_HEAD를_다시_읽지_않는다(tmp_path: Path) -> None:
    git = 가짜git()
    clock = {"t": 5000.0}
    sup = 감독(tmp_path, git, now=lambda: clock["t"], install_check_sec=60)
    git.head = NEW
    clock["t"] += 30
    sup.tick()
    assert not (tmp_path / "apply-request").exists()
    clock["t"] += 31
    sup.tick()
    assert (tmp_path / "apply-request").exists()


def test_설치본_루트는_패키지가_체크아웃_안에_있을_때만_찾는다(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    package = repo / "src" / "slack_cli_agent"
    package.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    assert install_root(package) == repo.resolve()

    stray = repo / "venv" / "site-packages" / "slack_cli_agent"
    stray.mkdir(parents=True)
    assert install_root(stray) is None
    assert install_root(tmp_path / "nowhere") is None


class 시계:
    def __init__(self, t: float = 5000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_진행_중_작업이_있으면_요청을_미루고_유휴가_되면_쓴다(tmp_path: Path) -> None:
    """요청을 쓰면 워커가 집기를 멈추고 TERM 이 긴 턴을 끊는다(11-07..11-13 턴 4건
    2,981초 손실). 그래서 진행 중 작업이 0일 때만 쓴다."""
    git = 가짜git()
    clock = 시계()
    running = {"n": 2}
    sup = 감독(tmp_path, git, now=clock, inflight=lambda: running["n"])
    git.head = NEW
    sup.tick()
    assert not (tmp_path / "apply-request").exists()
    clock.t += 600
    sup.tick()
    assert not (tmp_path / "apply-request").exists()
    running["n"] = 0
    sup.tick()
    request = ApplyRequest.parse((tmp_path / "apply-request").read_text())
    assert request is not None
    assert request.note == "설치본 갱신 aaaaaaa -> bbbbbbb"


def test_HEAD_재조회_간격과_무관하게_유휴를_매회차_본다(tmp_path: Path) -> None:
    git = 가짜git()
    clock = 시계()
    running = {"n": 1}
    sup = 감독(tmp_path, git, now=clock, inflight=lambda: running["n"], install_check_sec=60)
    git.head = NEW
    clock.t += 61
    sup.tick()
    assert not (tmp_path / "apply-request").exists()
    clock.t += 5
    running["n"] = 0
    sup.tick()
    assert (tmp_path / "apply-request").exists()


def test_대기_상한을_넘기면_진행_중이어도_쓴다(tmp_path: Path) -> None:
    """바쁜 봇도 결국 갱신된다. 상한을 넘긴 뒤는 기존 동작과 같다."""
    git = 가짜git()
    clock = 시계()
    sup = 감독(tmp_path, git, now=clock, inflight=lambda: 1, idle_max_wait_sec=1800)
    git.head = NEW
    sup.tick()
    clock.t += 1799
    sup.tick()
    assert not (tmp_path / "apply-request").exists()
    clock.t += 1
    sup.tick()
    request = ApplyRequest.parse((tmp_path / "apply-request").read_text())
    assert request is not None
    assert request.note.startswith("설치본 갱신 aaaaaaa -> bbbbbbb")
    assert "대기 상한" in request.note


def test_작업_수_조회가_실패하면_진행_중으로_본다(tmp_path: Path) -> None:
    def 실패() -> int:
        raise RuntimeError("조회 실패")

    git = 가짜git()
    clock = 시계()
    sup = 감독(tmp_path, git, now=clock, inflight=실패, idle_max_wait_sec=100)
    git.head = NEW
    sup.tick()
    assert not (tmp_path / "apply-request").exists()
    clock.t += 100
    sup.tick()
    assert (tmp_path / "apply-request").exists()


def test_기다리는_동안_main을_벗어나면_쓰지_않는다(tmp_path: Path) -> None:
    git = 가짜git()
    clock = 시계()
    running = {"n": 1}
    sup = 감독(tmp_path, git, now=clock, inflight=lambda: running["n"], install_check_sec=60)
    git.head = NEW
    clock.t += 61
    sup.tick()
    git.branch = "fix/x"
    running["n"] = 0
    clock.t += 5
    sup.tick()
    assert not (tmp_path / "apply-request").exists()


def test_기다리는_동안_턴이_쓴_요청은_건드리지_않는다(tmp_path: Path) -> None:
    """소유자가 턴에서 요청한 재기동은 기존 경로 그대로다. 그 재기동이 같은
    HEAD 를 싣으므로 기준만 옮긴다."""
    git = 가짜git()
    clock = 시계()
    running = {"n": 1}
    sup = 감독(tmp_path, git, now=clock, inflight=lambda: running["n"])
    git.head = NEW
    sup.tick()
    (tmp_path / "apply-request").write_text(f"{int(clock.t)}\nC1:1.2\n턴이 쓴 요청\n")
    running["n"] = 0
    sup.tick()
    assert "턴이 쓴 요청" in (tmp_path / "apply-request").read_text()
    (tmp_path / "apply-request").unlink()
    sup.tick()
    assert not (tmp_path / "apply-request").exists()


def test_HEAD가_기준으로_돌아오면_대기를_거둔다(tmp_path: Path) -> None:
    git = 가짜git()
    clock = 시계()
    running = {"n": 1}
    sup = 감독(tmp_path, git, now=clock, inflight=lambda: running["n"])
    git.head = NEW
    sup.tick()
    git.head = OLD
    running["n"] = 0
    sup.tick()
    assert not (tmp_path / "apply-request").exists()


def test_대기_상한_기본값은_1800초다() -> None:
    assert RuntimeSettings().restart_idle_max_wait_sec == 1800


def test_진행_중_작업은_박동이_살아_있는_RUNNING_만_센다(tmp_path: Path) -> None:
    """감독은 별도 프로세스라 워커의 inflight 를 못 본다. 같은 DB 를 읽는다."""
    import sqlite3

    from slack_cli_agent.reliability.supervisor import running_jobs
    from slack_cli_agent.storage.database import Database

    db = tmp_path / "state.db"
    assert running_jobs(db, now=1000.0, stale_sec=15) == 0
    Database(db).migrate()
    conn = sqlite3.connect(db)
    rows = [
        ("1.1", "RUNNING", 995.0),  # 박동 살아 있음
        ("1.2", "RUNNING", 900.0),  # 죽은 워커가 남긴 행
        ("1.3", "QUEUED", None),
        ("1.4", "COMPLETED", 999.0),
    ]
    for ts, status, beat in rows:
        conn.execute(
            "INSERT INTO jobs (channel, thread_ts, message_ts, user_id, payload, status,"
            " created_at, heartbeat_ts) VALUES ('C1', ?, ?, 'U1', '{}', ?, 0, ?)",
            (ts, ts, status, beat),
        )
    conn.commit()
    conn.close()
    assert running_jobs(db, now=1000.0, stale_sec=15) == 1
