"""단일 진입점 CLI.

하위 프로세스를 띄우지 않는다. `SlackCliAgent.run()` 에 argv 목록을 직접 넘겨
파서와 명령 객체를 그대로 호출한다 — 격리가 유지되고 빠르다.
"""

from __future__ import annotations

import io
import json
import signal
from pathlib import Path

import pytest

from slack_cli_agent.cli import IngressCommand, SlackCliAgent, WorkerCommand
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.lifecycle import InflightCounter
from slack_cli_agent.storage.database import Database

MINIMAL_PROFILE = {
    "name": "example",
    "primary_engine": {"type": "claude", "binary": "python3", "model": "m"},
    "owner_user_id": "U1",
    "troubleshoot_channel": "C1",
}


def write_profile(profiles_dir: Path, state_dir: Path, name: str = "example", **overrides) -> Path:
    profiles_dir.mkdir(parents=True, exist_ok=True)
    data = {**MINIMAL_PROFILE, "state_dir": str(state_dir), **overrides}
    path = profiles_dir / f"{name}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestPreflightCommand:
    def test_점검을_전부_통과하면_0을_돌려준다(self, tmp_path: Path) -> None:
        profiles = tmp_path / "profiles"
        state_dir = tmp_path.parent / "outside_home_state"
        work_root = state_dir / "work"
        work_root.mkdir(parents=True)
        write_profile(profiles, state_dir, work_root=str(work_root))
        out = io.StringIO()
        code = SlackCliAgent().run(
            ["preflight", "--profile", "example", "--profile-dir", str(profiles)], stdout=out
        )
        assert code == 0
        assert "기동 가능" in out.getvalue()

    def test_실행_파일이_없으면_1을_돌려준다(self, tmp_path: Path) -> None:
        profiles = tmp_path / "profiles"
        write_profile(
            profiles,
            tmp_path / "state",
            primary_engine={"type": "claude", "binary": str(tmp_path / "없는파일"), "model": "m"},
        )
        out = io.StringIO()
        code = SlackCliAgent().run(
            ["preflight", "--profile", "example", "--profile-dir", str(profiles)], stdout=out
        )
        assert code == 1
        assert "기동 불가" in out.getvalue()

    def test_점검_결과가_이름과_함께_한_줄씩_나온다(self, tmp_path: Path) -> None:
        profiles = tmp_path / "profiles"
        write_profile(
            profiles,
            tmp_path / "state",
            primary_engine={"type": "claude", "binary": str(tmp_path / "없는파일"), "model": "m"},
        )
        out = io.StringIO()
        SlackCliAgent().run(
            ["preflight", "--profile", "example", "--profile-dir", str(profiles)], stdout=out
        )
        assert "engine_binary" in out.getvalue()

    def test_프로필을_못_찾으면_알리고_2를_돌려준다(self, tmp_path: Path) -> None:
        out = io.StringIO()
        code = SlackCliAgent().run(
            ["preflight", "--profile", "없는프로필", "--profile-dir", str(tmp_path)], stdout=out
        )
        assert code == 2
        assert "없는프로필" in out.getvalue()


class TestMigrateCommand:
    def test_마이그레이션을_적용하고_버전을_알린다(self, tmp_path: Path) -> None:
        profiles = tmp_path / "profiles"
        state_dir = tmp_path / "state"
        write_profile(profiles, state_dir)
        out = io.StringIO()
        code = SlackCliAgent().run(
            ["migrate", "--profile", "example", "--profile-dir", str(profiles)], stdout=out
        )
        assert code == 0
        assert "적용" in out.getvalue()
        db = Database(state_dir / "state.db")
        assert db.connect().execute("PRAGMA user_version").fetchone()[0] > 0

    def test_이미_최신이면_다시_불러도_그대로임을_알린다(self, tmp_path: Path) -> None:
        profiles = tmp_path / "profiles"
        state_dir = tmp_path / "state"
        write_profile(profiles, state_dir)
        first = io.StringIO()
        SlackCliAgent().run(
            ["migrate", "--profile", "example", "--profile-dir", str(profiles)], stdout=first
        )
        second = io.StringIO()
        code = SlackCliAgent().run(
            ["migrate", "--profile", "example", "--profile-dir", str(profiles)], stdout=second
        )
        assert code == 0
        assert "이미 최신" in second.getvalue()


class TestChannelsCommand:
    def test_등록된_채널이_없으면_그렇게_알린다(self, tmp_path: Path) -> None:
        profiles = tmp_path / "profiles"
        write_profile(profiles, tmp_path / "state")
        out = io.StringIO()
        code = SlackCliAgent().run(
            ["channels", "--profile", "example", "--profile-dir", str(profiles)], stdout=out
        )
        assert code == 0
        assert "없다" in out.getvalue()

    def test_등록된_채널을_나열한다(self, tmp_path: Path) -> None:
        profiles = tmp_path / "profiles"
        state_dir = tmp_path / "state"
        write_profile(profiles, state_dir)
        channels_path = state_dir / "channels.json"
        channels_path.parent.mkdir(parents=True, exist_ok=True)
        channels_path.write_text(
            json.dumps({"C1": {"name": "일반", "mode": "default"}}), encoding="utf-8"
        )
        out = io.StringIO()
        code = SlackCliAgent().run(
            ["channels", "--profile", "example", "--profile-dir", str(profiles)], stdout=out
        )
        assert code == 0
        text = out.getvalue()
        assert "C1" in text
        assert "일반" in text


class TestCliParserContract:
    def test_명령어가_없으면_2로_끝난다(self) -> None:
        with pytest.raises(SystemExit) as exc:
            SlackCliAgent().run([])
        assert exc.value.code == 2

    def test_알_수_없는_명령어면_2로_끝난다(self) -> None:
        with pytest.raises(SystemExit) as exc:
            SlackCliAgent().run(["없는명령"])
        assert exc.value.code == 2

    def test_등록된_명령_이름_전부를_안다(self) -> None:
        cli = SlackCliAgent()
        assert {"preflight", "migrate", "channels"} <= set(cli.command_names())


class FakeGateway:
    """게이트웨이 이중체. start 호출 여부와 인자만 기록한다."""

    def __init__(self, raise_on_start: Exception | None = None) -> None:
        self.start_calls: list[str] = []
        self._raise_on_start = raise_on_start

    def start(self, app_token: str) -> None:
        self.start_calls.append(app_token)
        if self._raise_on_start is not None:
            raise self._raise_on_start


class FakeIngress:
    """등록 대상 게이트웨이를 기억해 두는 이중체."""

    def __init__(self) -> None:
        self.registered_with: object | None = None

    def register(self, gateway: object) -> None:
        self.registered_with = gateway


class FakeRefresher:
    """주기 실행기의 시작·정지 호출을 기록하는 대역."""

    def __init__(self) -> None:
        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1


class FakeApplication:
    """IngressCommand/WorkerCommand 가 기대하는 계약만 흉내 낸 이중체."""

    def __init__(
        self,
        gateway: FakeGateway | None = None,
        ingress: FakeIngress | None = None,
        worker: object | None = None,
        channel_ids: list[str] | None = None,
    ) -> None:
        self._gateway = gateway if gateway is not None else FakeGateway()
        self._ingress = ingress if ingress is not None else FakeIngress()
        self._worker = worker
        self._channel_ids = channel_ids if channel_ids is not None else []
        self.gateway_calls = 0
        self.close_calls = 0
        self.connection_watch_calls = 0
        self._roster_refresher = FakeRefresher()
        self.snapshot_runner = FakeRefresher()
        self.watch_runner_ = FakeRefresher()
        self.health_runner_ = FakeRefresher()
        self.purge_runner_ = FakeRefresher()
        # 접수 프로세스가 재기동 동작을 무엇으로 넘겼는지 기록한다.
        self.health_restart: object | None = None
        self.restarter_calls = 0
        self.shutdown_marks = 0
        # 종료 대기 상한을 여기서 가져간다. 실제 Application 과 같은 계약이다.
        self.settings = RuntimeSettings()

    def state_snapshot_runner(self) -> FakeRefresher:
        return self.snapshot_runner

    def watch_runner(self) -> FakeRefresher:
        return self.watch_runner_

    def job_purge_runner(self) -> FakeRefresher:
        return self.purge_runner_

    def health_runner(self, restart: object) -> FakeRefresher:
        self.health_restart = restart
        return self.health_runner_

    def self_restarter(self) -> object:
        self.restarter_calls += 1
        return lambda reason: None

    def mark_shutting_down(self) -> None:
        self.shutdown_marks += 1

    def roster_refresher(self) -> FakeRefresher:
        return self._roster_refresher

    def connection_watch(self) -> object:
        self.connection_watch_calls += 1
        return object()

    def gateway(self) -> FakeGateway:
        self.gateway_calls += 1
        return self._gateway

    def ingress(self) -> FakeIngress:
        return self._ingress

    def worker(self, worker_id: str) -> object:
        return self._worker

    def channel_ids(self) -> list[str]:
        return self._channel_ids

    def close(self) -> None:
        self.close_calls += 1


class TestIngressRosterRefresh:
    """접수 프로세스가 명부 갱신기를 시작하는가.

    명부 파일을 소비하는 것은 워커의 프롬프트 조립이지만, 갱신은 접수
    프로세스가 맡는다. 워커는 여럿 뜰 수 있어 거기서 돌리면 같은 파일을
    여러 프로세스가 동시에 쓴다. 접수는 소켓 연결 하나당 하나다.

    시작하지 않으면 명부 파일이 만들어지지 않고, 프롬프트 절은 늘 빈
    문자열을 낸다 — 기능이 연결된 것과 동작하는 것은 다르다.
    """

    def test_기동_전에_명부_갱신기를_시작한다(self, tmp_path: Path) -> None:
        profiles = tmp_path / "profiles"
        write_profile(profiles, tmp_path / "state")
        app = FakeApplication()
        cli = SlackCliAgent([IngressCommand(application_factory=lambda profile: app)])
        cli.run(
            ["ingress", "--profile", "example", "--profile-dir", str(profiles), "--app-token", "xapp-토큰"],
            stdout=io.StringIO(),
        )
        assert app.roster_refresher().start_calls == 1

    def test_토큰이_없으면_갱신기를_시작하지_않는다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
        profiles = tmp_path / "profiles"
        write_profile(profiles, tmp_path / "state")
        app = FakeApplication()
        cli = SlackCliAgent([IngressCommand(application_factory=lambda profile: app)])
        code = cli.run(
            ["ingress", "--profile", "example", "--profile-dir", str(profiles)],
            stdout=io.StringIO(),
        )
        assert code == 2
        assert app.roster_refresher().start_calls == 0


class TestIngressConnectionWatch:
    """접수 프로세스가 연결 감시를 켜는가.

    감시를 안 켜면 소켓이 끊겨도 아무것도 세지 않는다. 소켓 연결은 접수
    프로세스에만 있으므로 여기서 켜지 않으면 어디서도 안 켜진다.
    """

    def test_기동_전에_연결_감시를_켠다(self, tmp_path: Path) -> None:
        profiles = tmp_path / "profiles"
        write_profile(profiles, tmp_path / "state")
        app = FakeApplication()
        cli = SlackCliAgent([IngressCommand(application_factory=lambda profile: app)])
        cli.run(
            ["ingress", "--profile", "example", "--profile-dir", str(profiles), "--app-token", "xapp-토큰"],
            stdout=io.StringIO(),
        )
        assert app.connection_watch_calls == 1

    def test_토큰이_없으면_감시를_켜지_않는다(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """토큰이 없으면 기동 자체를 안 하므로 감시도 안 켠다.

        켜 두면 로거에 핸들러만 붙고 그 프로세스는 곧 끝난다.
        """
        monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
        profiles = tmp_path / "profiles"
        write_profile(profiles, tmp_path / "state")
        app = FakeApplication()
        cli = SlackCliAgent([IngressCommand(application_factory=lambda profile: app)])
        code = cli.run(
            ["ingress", "--profile", "example", "--profile-dir", str(profiles)],
            stdout=io.StringIO(),
        )
        assert code == 2
        assert app.connection_watch_calls == 0


class FakeWorker:
    """WorkerCommand 가 부르는 순서를 기록하는 이중체."""

    def __init__(self, stop_after_run_once: int | None = None) -> None:
        # 종료 절차가 이 값을 본다. 실제 Worker 와 같은 계약이다.
        self.inflight = InflightCounter()
        self.calls: list[str] = []
        self.reclaim_calls = 0
        self.run_once_calls = 0
        self.catch_up_calls: list[list[str]] = []
        self.shutdown_calls = 0
        self._stop_after_run_once = stop_after_run_once
        # run_forever 가 받은 중단 판정 함수. 조립이 종료 신호를 실제로
        # 넘겼는지 본다.
        self.stop_checks: list[object] = []

    def run_forever(self, should_stop) -> None:
        self.stop_checks.append(should_stop)
        self.calls.append("run_forever")
        while not should_stop():
            self.run_once()

    def reclaim(self) -> None:
        self.reclaim_calls += 1
        self.calls.append("reclaim")

    def run_once(self) -> bool:
        self.run_once_calls += 1
        self.calls.append("run_once")
        if self._stop_after_run_once is not None and self.run_once_calls >= self._stop_after_run_once:
            raise KeyboardInterrupt
        return True

    def catch_up(self, channels: list[str]) -> None:
        self.catch_up_calls.append(channels)
        self.calls.append("catch_up")

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.calls.append("shutdown")


class TestIngressCommand:
    def test_등록후_시작한다(self, tmp_path: Path) -> None:
        profiles = tmp_path / "profiles"
        write_profile(profiles, tmp_path / "state")
        app = FakeApplication()
        cli = SlackCliAgent([IngressCommand(application_factory=lambda profile: app)])
        out = io.StringIO()
        code = cli.run(
            [
                "ingress",
                "--profile",
                "example",
                "--profile-dir",
                str(profiles),
                "--app-token",
                "xapp-토큰",
            ],
            stdout=out,
        )
        assert code == 0
        assert app._ingress.registered_with is app._gateway
        assert app._gateway.start_calls == ["xapp-토큰"]

    def test_게이트웨이를_한번만_얻는다(self, tmp_path: Path) -> None:
        profiles = tmp_path / "profiles"
        write_profile(profiles, tmp_path / "state")
        app = FakeApplication()
        cli = SlackCliAgent([IngressCommand(application_factory=lambda profile: app)])
        out = io.StringIO()
        cli.run(
            ["ingress", "--profile", "example", "--profile-dir", str(profiles), "--app-token", "xapp-토큰"],
            stdout=out,
        )
        assert app.gateway_calls == 1

    def test_토큰이_없으면_시작하지_않고_2를_돌려준다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
        profiles = tmp_path / "profiles"
        write_profile(profiles, tmp_path / "state")
        app = FakeApplication()
        cli = SlackCliAgent([IngressCommand(application_factory=lambda profile: app)])
        out = io.StringIO()
        code = cli.run(
            ["ingress", "--profile", "example", "--profile-dir", str(profiles)], stdout=out
        )
        assert code == 2
        assert app._gateway.start_calls == []

    def test_환경변수만_있어도_기동한다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-환경변수")
        profiles = tmp_path / "profiles"
        write_profile(profiles, tmp_path / "state")
        app = FakeApplication()
        cli = SlackCliAgent([IngressCommand(application_factory=lambda profile: app)])
        out = io.StringIO()
        code = cli.run(
            ["ingress", "--profile", "example", "--profile-dir", str(profiles)], stdout=out
        )
        assert code == 0
        assert app._gateway.start_calls == ["xapp-환경변수"]

    def test_시작이_예외를_내도_close가_불린다(self, tmp_path: Path) -> None:
        profiles = tmp_path / "profiles"
        write_profile(profiles, tmp_path / "state")
        gateway = FakeGateway(raise_on_start=RuntimeError("연결 실패"))
        app = FakeApplication(gateway=gateway)
        cli = SlackCliAgent([IngressCommand(application_factory=lambda profile: app)])
        out = io.StringIO()
        with pytest.raises(RuntimeError):
            cli.run(
                [
                    "ingress",
                    "--profile",
                    "example",
                    "--profile-dir",
                    str(profiles),
                    "--app-token",
                    "xapp-토큰",
                ],
                stdout=out,
            )
        assert app.close_calls == 1


class TestWorkerCommand:
    def test_reclaim_먼저_부르고_처리를_시작한다(self, tmp_path: Path) -> None:
        """붙잡힌 채 남은 작업을 되돌리기 전에 새 작업을 집으면 안 된다."""
        profiles = tmp_path / "profiles"
        write_profile(profiles, tmp_path / "state")
        worker = FakeWorker(stop_after_run_once=1)
        app = FakeApplication(worker=worker)
        cli = SlackCliAgent([WorkerCommand(application_factory=lambda profile: app)])
        out = io.StringIO()
        code = cli.run(
            ["worker", "--profile", "example", "--profile-dir", str(profiles)], stdout=out
        )
        assert code == 0
        assert worker.calls[:2] == ["reclaim", "run_forever"]

    def test_once_주면_run_once가_한번만_불린다(self, tmp_path: Path) -> None:
        profiles = tmp_path / "profiles"
        write_profile(profiles, tmp_path / "state")
        worker = FakeWorker()
        app = FakeApplication(worker=worker)
        cli = SlackCliAgent([WorkerCommand(application_factory=lambda profile: app)])
        out = io.StringIO()
        code = cli.run(
            ["worker", "--profile", "example", "--profile-dir", str(profiles), "--once"], stdout=out
        )
        assert code == 0
        assert worker.run_once_calls == 1

    def test_catch_up_없으면_안_부른다(self, tmp_path: Path) -> None:
        profiles = tmp_path / "profiles"
        write_profile(profiles, tmp_path / "state")
        worker = FakeWorker()
        app = FakeApplication(worker=worker)
        cli = SlackCliAgent([WorkerCommand(application_factory=lambda profile: app)])
        out = io.StringIO()
        cli.run(["worker", "--profile", "example", "--profile-dir", str(profiles), "--once"], stdout=out)
        assert worker.catch_up_calls == []

    def test_catch_up_주면_부른다(self, tmp_path: Path) -> None:
        profiles = tmp_path / "profiles"
        write_profile(profiles, tmp_path / "state")
        worker = FakeWorker()
        app = FakeApplication(worker=worker, channel_ids=["C1", "C2"])
        cli = SlackCliAgent([WorkerCommand(application_factory=lambda profile: app)])
        out = io.StringIO()
        cli.run(
            ["worker", "--profile", "example", "--profile-dir", str(profiles), "--once", "--catch-up"],
            stdout=out,
        )
        assert worker.catch_up_calls == [["C1", "C2"]]

    def test_KeyboardInterrupt가_나면_shutdown이_불리고_0을_돌려준다(self, tmp_path: Path) -> None:
        profiles = tmp_path / "profiles"
        write_profile(profiles, tmp_path / "state")
        worker = FakeWorker(stop_after_run_once=2)
        app = FakeApplication(worker=worker)
        cli = SlackCliAgent([WorkerCommand(application_factory=lambda profile: app)])
        out = io.StringIO()
        code = cli.run(
            ["worker", "--profile", "example", "--profile-dir", str(profiles)], stdout=out
        )
        assert code == 0
        assert worker.run_once_calls == 2
        assert worker.shutdown_calls == 1

    def test_shutdown과_close가_한번씩만_불린다(self, tmp_path: Path) -> None:
        profiles = tmp_path / "profiles"
        write_profile(profiles, tmp_path / "state")
        worker = FakeWorker()
        app = FakeApplication(worker=worker)
        cli = SlackCliAgent([WorkerCommand(application_factory=lambda profile: app)])
        out = io.StringIO()
        cli.run(["worker", "--profile", "example", "--profile-dir", str(profiles), "--once"], stdout=out)
        assert worker.shutdown_calls == 1
        assert app.close_calls == 1


class TestWorkerCommand종료신호:
    """SIGTERM 을 받았을 때 처리 중인 요청을 마치고 끝나는가.

    지금까지는 KeyboardInterrupt(SIGINT)만 받았다. 배포 스크립트와
    프로세스 관리자는 SIGTERM 을 보내므로, 그 신호를 안 받으면 워커가
    그 자리에서 죽는다 — 집어 둔 작업이 큐에 잡힌 채로 남아 다음
    워커가 정체 판정 시각까지 그 작업을 못 집는다.
    """

    def _run(self, tmp_path: Path, worker: FakeWorker, registered: list) -> int:
        profiles = tmp_path / "profiles"
        write_profile(profiles, tmp_path / "state")
        app = FakeApplication(worker=worker)
        command = WorkerCommand(
            application_factory=lambda profile: app,
            signal_register=lambda sig, handler: registered.append((sig, handler)),
        )
        return SlackCliAgent([command]).run(
            ["worker", "--profile", "example", "--profile-dir", str(profiles)], stdout=io.StringIO()
        )

    def test_SIGTERM과_SIGINT를_등록한다(self, tmp_path: Path) -> None:
        registered: list = []
        self._run(tmp_path, FakeWorker(stop_after_run_once=1), registered)
        assert [sig for sig, _ in registered] == [signal.SIGTERM, signal.SIGINT]

    def test_종료_신호를_받으면_반복이_끝나고_정리한다(self, tmp_path: Path) -> None:
        registered: list = []
        worker = FakeWorker()
        # 한 번 처리한 뒤 종료 신호가 온 상황을 만든다.
        original_run_once = worker.run_once

        def 신호와_함께(self_worker=worker):
            result = original_run_once()
            for _sig, handler in registered:
                handler(15, None)
            return result

        worker.run_once = 신호와_함께  # type: ignore[method-assign]
        code = self._run(tmp_path, worker, registered)
        assert code == 0
        assert worker.run_once_calls == 1
        assert worker.shutdown_calls == 1


class TestPreflight소유자설정경고:
    """소유자에게 적용되지 않는 채널 설정을 기동 점검이 실제로 알리는가.

    점검 항목을 만들기만 하고 목록에 안 넣으면 어떤 기동에서도 안 돈다.
    설정을 바꿔 두고 왜 안 바뀌는지 찾게 된다.
    """

    def test_점검_목록에_들어간다(self, tmp_path: Path) -> None:
        from slack_cli_agent.preflight.checks import OwnerSettingsInertCheck

        profiles = tmp_path / "profiles"
        write_profile(profiles, tmp_path / "state")
        out = io.StringIO()
        SlackCliAgent().run(
            ["preflight", "--profile", "example", "--profile-dir", str(profiles)], stdout=out
        )
        assert OwnerSettingsInertCheck().name in out.getvalue()


class TestWorkerCommand상태기록:
    """워커가 도는 동안 상태 기록이 실제로 갱신되는가.

    기록기를 만들어도 주기 실행에 안 걸면 파일이 한 번도 안 바뀐다.
    """

    def test_주기_실행기를_띄우고_종료_때_멈춘다(self, tmp_path: Path) -> None:
        profiles = tmp_path / "profiles"
        write_profile(profiles, tmp_path / "state")
        app = FakeApplication(worker=FakeWorker(stop_after_run_once=1))
        command = WorkerCommand(
            application_factory=lambda profile: app,
            signal_register=lambda sig, handler: None,
        )
        SlackCliAgent([command]).run(
            ["worker", "--profile", "example", "--profile-dir", str(profiles)], stdout=io.StringIO()
        )
        assert app.snapshot_runner.start_calls == 1
        assert app.snapshot_runner.stop_calls == 1

    def test_종료_신호를_상태_기록에_알린다(self, tmp_path: Path) -> None:
        """알리지 않으면 상태 파일만 보는 쪽이 멈춘 프로세스와 종료 중인
        프로세스를 구분하지 못한다."""
        profiles = tmp_path / "profiles"
        write_profile(profiles, tmp_path / "state")
        app = FakeApplication(worker=FakeWorker(stop_after_run_once=1))
        registered: list = []
        command = WorkerCommand(
            application_factory=lambda profile: app,
            signal_register=lambda sig, handler: registered.append(handler),
        )
        SlackCliAgent([command]).run(
            ["worker", "--profile", "example", "--profile-dir", str(profiles)], stdout=io.StringIO()
        )
        registered[0](signal.SIGTERM, None)
        assert app.shutdown_marks == 1


class TestWorkerCommand감시확인:
    """등록된 감시 건을 주기적으로 확인하는 실행기가 워커에서 도는가.

    등록 경로만 있고 이 실행기를 안 띄우면 큐에 들어간 건을 아무도 확인하지 않는다.
    """

    def test_주기_실행기를_띄우고_종료_때_멈춘다(self, tmp_path: Path) -> None:
        profiles = tmp_path / "profiles"
        write_profile(profiles, tmp_path / "state")
        app = FakeApplication(worker=FakeWorker(stop_after_run_once=1))
        command = WorkerCommand(
            application_factory=lambda profile: app,
            signal_register=lambda sig, handler: None,
        )
        SlackCliAgent([command]).run(
            ["worker", "--profile", "example", "--profile-dir", str(profiles)], stdout=io.StringIO()
        )
        assert app.watch_runner_.start_calls == 1
        assert app.watch_runner_.stop_calls == 1

    def test_한_번만_처리하는_실행에서도_멈춘다(self, tmp_path: Path) -> None:
        """멈추지 않으면 워커가 끝나도 감시 스레드가 남아 프로세스가 안 끝난다."""
        profiles = tmp_path / "profiles"
        write_profile(profiles, tmp_path / "state")
        app = FakeApplication(worker=FakeWorker())
        command = WorkerCommand(
            application_factory=lambda profile: app,
            signal_register=lambda sig, handler: None,
        )
        SlackCliAgent([command]).run(
            ["worker", "--profile", "example", "--profile-dir", str(profiles), "--once"],
            stdout=io.StringIO(),
        )
        assert app.watch_runner_.stop_calls == 1


class TestIngress연결점검주기:
    """접수 프로세스가 연결 점검을 주기적으로 실행하는가.

    오류를 세는 핸들러를 붙이는 것과 그 값이 상한을 넘었는지 보는 것은 다른
    일이다. 점검 주기가 안 돌면 소켓 오류가 아무리 발생해도 재기동이
    발화하지 않는다.
    """

    @staticmethod
    def _기동한다(tmp_path: Path, app: FakeApplication, token: bool = True) -> int:
        profiles = tmp_path / "profiles"
        write_profile(profiles, tmp_path / "state")
        cli = SlackCliAgent([IngressCommand(application_factory=lambda profile: app)])
        argv = ["ingress", "--profile", "example", "--profile-dir", str(profiles)]
        if token:
            argv += ["--app-token", "xapp-토큰"]
        return cli.run(argv, stdout=io.StringIO())

    def test_기동하면_점검_주기실행기를_시작한다(self, tmp_path: Path) -> None:
        app = FakeApplication()
        self._기동한다(tmp_path, app)
        assert app.health_runner_.start_calls == 1

    def test_끝날때_점검_주기실행기를_멈춘다(self, tmp_path: Path) -> None:
        """안 멈추면 종료 절차가 끝난 뒤에도 그 스레드가 슬랙 API 를 계속 부른다."""
        app = FakeApplication()
        self._기동한다(tmp_path, app)
        assert app.health_runner_.stop_calls == 1

    def test_재기동_동작을_함께_넘긴다(self, tmp_path: Path) -> None:
        """넘기지 않으면 판정만 나오고 아무 일도 일어나지 않는다."""
        app = FakeApplication()
        self._기동한다(tmp_path, app)
        assert app.restarter_calls == 1
        assert callable(app.health_restart)

    def test_토큰이_없으면_시작하지_않는다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
        app = FakeApplication()
        assert self._기동한다(tmp_path, app, token=False) == 2
        assert app.health_runner_.start_calls == 0


class TestWorker빈큐대기:
    """워커 명령이 빈 큐를 쉬지 않고 조회하지 않는가.

    `run_once()` 를 그대로 되풀이하면 큐가 비어 있어도 SQLite 조회가 계속
    일어난다. 대기를 포함한 반복은 워커가 맡으므로, 조립은 그 경로를 써야
    한다 — 여기서 직접 반복하면 대기가 빠진다.
    """

    @staticmethod
    def _돌린다(tmp_path: Path, worker: FakeWorker, once: bool = False) -> int:
        profiles = tmp_path / "profiles"
        write_profile(profiles, tmp_path / "state")
        app = FakeApplication(worker=worker)
        cli = SlackCliAgent([
            WorkerCommand(
                application_factory=lambda profile: app,
                signal_register=lambda signum, handler: None,
            )
        ])
        argv = ["worker", "--profile", "example", "--profile-dir", str(profiles)]
        if once:
            argv.append("--once")
        return cli.run(argv, stdout=io.StringIO())

    def test_반복은_워커에_맡긴다(self, tmp_path: Path) -> None:
        worker = FakeWorker(stop_after_run_once=2)
        self._돌린다(tmp_path, worker)
        assert "run_forever" in worker.calls

    def test_종료신호를_중단_판정으로_넘긴다(self, tmp_path: Path) -> None:
        """안 넘기면 종료 신호를 받아도 반복이 안 끝난다."""
        worker = FakeWorker(stop_after_run_once=2)
        self._돌린다(tmp_path, worker)
        assert worker.stop_checks and callable(worker.stop_checks[0])

    def test_once_면_한_번만_처리한다(self, tmp_path: Path) -> None:
        worker = FakeWorker()
        self._돌린다(tmp_path, worker, once=True)
        assert worker.run_once_calls == 1
        assert "run_forever" not in worker.calls


class TestWorker끝난작업정리:
    """워커가 끝난 작업 정리를 주기적으로 실행하는가.

    안 띄우면 완료·실패 행이 계속 남아 jobs 표가 무한히 커진다. 워커가
    여럿 떠도 삭제는 조건이 같아 서로 어긋나지 않는다.
    """

    @staticmethod
    def _돌린다(tmp_path: Path, worker: FakeWorker) -> int:
        profiles = tmp_path / "profiles"
        write_profile(profiles, tmp_path / "state")
        app = FakeApplication(worker=worker)
        cli = SlackCliAgent([
            WorkerCommand(
                application_factory=lambda profile: app,
                signal_register=lambda signum, handler: None,
            )
        ])
        code = cli.run(
            ["worker", "--profile", "example", "--profile-dir", str(profiles)],
            stdout=io.StringIO(),
        )
        TestWorker끝난작업정리.app = app
        return code

    def test_기동하면_정리를_시작한다(self, tmp_path: Path) -> None:
        worker = FakeWorker(stop_after_run_once=1)
        self._돌린다(tmp_path, worker)
        assert self.app.purge_runner_.start_calls == 1

    def test_끝날때_정리를_멈춘다(self, tmp_path: Path) -> None:
        worker = FakeWorker(stop_after_run_once=1)
        self._돌린다(tmp_path, worker)
        assert self.app.purge_runner_.stop_calls == 1
