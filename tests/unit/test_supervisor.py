"""재기동 요청 감독 (H2).

턴은 apply-request 표식만 쓴다. 감독은 launchd 가 샌드박스 밖에서 띄워
표식을 집고, 재기동하고, PID 가 바뀌었는지로 성공을 판정하고, 아무것도
안 돌면 되돌리고, 결과를 소유자 DM 으로 알린다. 지금까지는 마메치
watchdog.py 의 take_over_sca_apply 가 이 일을 대행했다.
"""

from __future__ import annotations

import os
from pathlib import Path

from slack_cli_agent.reliability.supervisor import ApplySupervisor, Service


class 가짜환경:
    """시계, launchctl, 프로세스 생존을 한곳에서 흉내 낸다."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.t = 1000.0
        self.살아있음: set[str] = {"11", "21"}
        self.kick: list[str] = []
        self.dm: list[str] = []
        self.다음pid = 100
        self.교체함 = True
        self.되돌림 = 0
        self.services = [
            Service("local.t.worker", root / "worker.pid"),
            Service("local.t.ingress", root / "ingress.pid"),
        ]
        self.services[0].pid_path.write_text("11\n")
        self.services[1].pid_path.write_text("21\n")

    def now(self) -> float:
        return self.t

    def sleep(self, sec: float) -> None:
        self.t += sec

    def kickstart(self, label: str) -> bool:
        self.kick.append(label)
        service = next(s for s in self.services if s.label == label)
        old = service.pid_path.read_text().strip() if service.pid_path.exists() else ""
        self.살아있음.discard(old)
        if self.교체함:
            self.다음pid += 1
            service.pid_path.write_text(f"{self.다음pid}\n")
            self.살아있음.add(str(self.다음pid))
        else:
            service.pid_path.unlink(missing_ok=True)
        return True

    def rollback(self) -> bool:
        self.되돌림 += 1
        self.교체함 = True
        return True

    def supervisor(self, **kw: object) -> ApplySupervisor:
        args: dict[str, object] = {
            "request_path": self.root / "apply-request",
            "running_path": self.root / "apply-running",
            "services": self.services,
            "kickstart": self.kickstart,
            "alive": lambda pid: pid in self.살아있음,
            "notify": self.dm.append,
            "now": self.now,
            "sleep": self.sleep,
            "grace_sec": 15,
            "confirm_timeout_sec": 60,
        }
        args.update(kw)
        return ApplySupervisor(**args)  # type: ignore[arg-type]

    def 표식(self, age: float) -> Path:
        path = self.root / "apply-request"
        path.write_text(f"{int(self.t - age)}\nC1:111.2\n")
        os.utime(path, (self.t - age, self.t - age))
        return path


def test_유예_전의_표식은_집지_않는다(tmp_path: Path) -> None:
    env = 가짜환경(tmp_path)
    env.표식(age=5)
    assert env.supervisor().tick() is None
    assert (tmp_path / "apply-request").exists()
    assert env.kick == []


def test_표식을_집어_재기동하고_PID_교체로_완료를_알린다(tmp_path: Path) -> None:
    env = 가짜환경(tmp_path)
    env.표식(age=20)
    started = env.t
    assert env.supervisor().tick() == "restarted"
    assert env.kick == ["local.t.worker", "local.t.ingress"]
    assert not (tmp_path / "apply-request").exists()
    assert not (tmp_path / "apply-running").exists()
    assert len(env.dm) == 1 and "재기동 완료" in env.dm[0]
    assert "C1:111.2" in env.dm[0]
    assert env.t - started < 60


def test_PID가_그대로면_완료로_보지_않고_살아있으면_되돌리지_않는다(tmp_path: Path) -> None:
    env = 가짜환경(tmp_path)
    env.표식(age=20)

    def 무시(label: str) -> bool:
        env.kick.append(label)
        return True

    sup = env.supervisor(kickstart=무시, rollback=env.rollback)
    assert sup.tick() == "failed_running"
    assert env.되돌림 == 0
    assert "되돌리지 않았" in env.dm[0]


def test_아무것도_안_돌면_되돌리고_다시_띄운다(tmp_path: Path) -> None:
    env = 가짜환경(tmp_path)
    env.교체함 = False
    env.표식(age=20)
    assert env.supervisor(rollback=env.rollback).tick() == "rolled_back"
    assert env.되돌림 == 1
    assert env.kick.count("local.t.worker") == 2
    assert "되돌렸" in env.dm[0]


def test_되돌릴_수단이_없으면_그렇게_알린다(tmp_path: Path) -> None:
    env = 가짜환경(tmp_path)
    env.교체함 = False
    env.표식(age=20)
    assert env.supervisor().tick() == "failed_down"
    assert "되돌릴 수단이 없" in env.dm[0]


def test_진행_중_점검이_있으면_미룬다(tmp_path: Path) -> None:
    env = 가짜환경(tmp_path)
    env.표식(age=20)
    assert env.supervisor(busy=lambda: 2).tick() == "deferred"
    assert (tmp_path / "apply-request").exists()
    assert env.kick == []


def test_끊긴_실행은_TTL_뒤_다시_집는다(tmp_path: Path) -> None:
    env = 가짜환경(tmp_path)
    running = tmp_path / "apply-running"
    running.write_text(f"{int(env.t)}\n")
    os.utime(running, (env.t - 10, env.t - 10))
    assert env.supervisor(run_ttl_sec=900).tick() is None
    os.utime(running, (env.t - 1000, env.t - 1000))
    assert env.supervisor(run_ttl_sec=900).tick() == "restarted"


def test_재기동_중에_들어온_새_표식은_지우지_않는다(tmp_path: Path) -> None:
    env = 가짜환경(tmp_path)
    env.표식(age=20)
    원래 = env.kickstart

    def 중간에_새요청(label: str) -> bool:
        (tmp_path / "apply-request").write_text("9999\n")
        return 원래(label)

    assert env.supervisor(kickstart=중간에_새요청).tick() == "restarted"
    assert (tmp_path / "apply-request").exists()


def test_알림이_실패해도_감독은_계속한다(tmp_path: Path) -> None:
    env = 가짜환경(tmp_path)
    env.표식(age=20)

    def 터짐(text: str) -> None:
        raise RuntimeError("slack down")

    assert env.supervisor(notify=터짐).tick() == "restarted"
    assert not (tmp_path / "apply-running").exists()


def test_진행_중_점검을_센다(tmp_path: Path) -> None:
    import json
    import sqlite3

    from slack_cli_agent.reliability.supervisor import active_reviews

    db = tmp_path / "state.db"
    assert active_reviews(db, now=5000.0) == 0
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE reviews (kind TEXT, channel TEXT, target_ts TEXT, at REAL, result TEXT)"
        )
        rows = [
            ("진행", 4900.0),
            ("완료", 4900.0),
            ("진행", 1000.0),  # 오래된 행은 죽은 점검이다
        ]
        for i, (status, at) in enumerate(rows):
            conn.execute(
                "INSERT INTO reviews VALUES ('k', 'C', ?, ?, ?)",
                (str(i), at, json.dumps({"status": status})),
            )
        conn.execute("INSERT INTO reviews VALUES ('k', 'C', 'x', 4900, 'not json')")
    assert active_reviews(db, now=5000.0) == 1


def test_supervise_명령이_등록돼_있고_once_로_한_번_돈다(tmp_path: Path) -> None:
    import io
    import json

    from slack_cli_agent.cli import SlackCliAgent, SuperviseCommand

    (tmp_path / "t.json").write_text(
        json.dumps(
            {
                "name": "t",
                "state_dir": str(tmp_path / "s"),
                "owner_user_id": "U1",
                "primary_engine": {"type": "claude", "binary": "python3", "model": "m"},
            }
        ),
        encoding="utf-8",
    )
    calls: list[str] = []

    class 가짜감독:
        def tick(self) -> str | None:
            calls.append("tick")
            return None

    cmd = SuperviseCommand(supervisor_factory=lambda profile, args: 가짜감독())
    out = io.StringIO()
    code = SlackCliAgent([cmd]).run(
        ["supervise", "--profile", "t", "--profile-dir", str(tmp_path), "--once"], out
    )
    assert code == 0
    assert calls == ["tick"]
    assert "할 일 없음" in out.getvalue()
    assert "supervise" in SlackCliAgent().command_names()
