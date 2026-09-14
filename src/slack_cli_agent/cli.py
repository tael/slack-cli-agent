"""단일 진입점 CLI.

원본 저장소는 봇 하나를 다루는 데도 셸 스크립트가 여러 개였다(`restart.sh`,
`apply.sh`, `run.sh` 등). 이 패키지는 프로필이 여럿(여러 봇)일 수 있게
재구성했으므로, 사람이 손으로 부르는 진입점도 셸이 아니라 하나의 파이썬 CLI로
모은다. 하위 명령은 클래스로 만들고 공통 계약은 `CliCommand` 가 정한다 —
`if name == ...` 분기 나열을 쓰지 않는다.

프로세스를 실제로 띄우는 하위 명령(ingress/worker 기동, 원본의 `run.sh`,
`restart.sh` 뒷부분)은 application factory 를 주입받는 구조로 되어 있다.
`core.application` 모듈은 슬랙 SDK 를 끌어오므로, 그 모듈의 import 는
기본 팩토리 함수 안으로 미뤄 둔다 — 그래야 이 모듈을 불러오는 것만으로
슬랙 SDK 가 딸려 들어오지 않고, preflight 같은 다른 하위 명령이 그것 때문에
막히는 일이 없다.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, ClassVar, TextIO

from .config.channel import ChannelRegistry
from .config.profile import Profile
from .core.errors import AgentError
from .core.lifecycle import GracefulShutdown, SignalRegister
from .preflight.check import PreflightContext
from .preflight.checks import (
    EngineBinaryCheck,
    McpServerCheck,
    OwnerSettingsInertCheck,
    PromptFileCheck,
    WorkdirCheck,
)
from .preflight.runner import PreflightRunner
from .storage.database import Database


class CliCommand(ABC):
    """하위 명령 하나의 계약."""

    name: ClassVar[str]
    help: ClassVar[str] = ""

    @abstractmethod
    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """이 명령이 받는 인자를 등록한다."""

    @abstractmethod
    def execute(self, args: argparse.Namespace, stdout: TextIO) -> int:
        """명령을 실행한다. 종료 코드를 돌려준다."""


class ProfileAwareCommand(CliCommand):
    """프로필을 읽어야 하는 하위 명령의 공통 부분.

    `--profile`, `--profile-dir` 는 모든 하위 명령이 똑같이 받는다. 프로필을
    찾지 못하면 무엇을 하든 이어갈 수 없으므로, 여기서 한 번만 처리해
    구체 명령은 이미 로드된 `Profile` 만 받는다.
    """

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--profile", required=True, help="프로필 이름")
        parser.add_argument(
            "--profile-dir",
            action="append",
            default=None,
            help="프로필 파일(<이름>.json)을 찾을 디렉터리. 여러 번 줄 수 있다. 기본은 현재 디렉터리",
        )
        self.add_command_arguments(parser)

    @abstractmethod
    def add_command_arguments(self, parser: argparse.ArgumentParser) -> None:
        """이 명령만의 인자. 없으면 빈 구현으로 둔다."""

    def execute(self, args: argparse.Namespace, stdout: TextIO) -> int:
        search_dirs = [Path(p) for p in (args.profile_dir or [Path.cwd()])]
        profile = Profile.load(args.profile, search_dirs)
        return self.execute_with_profile(profile, args, stdout)

    @abstractmethod
    def execute_with_profile(self, profile: Profile, args: argparse.Namespace, stdout: TextIO) -> int:
        """프로필을 이미 읽은 상태에서 이어갈 부분."""


class PreflightCommand(ProfileAwareCommand):
    """기동 전 점검을 실행한다. 원본 `restart.sh` 앞머리 + `check.py` 를 합친 것.

    구문 검사·단위 시험 실행(원본 `restart.sh` 가 하던 것)은 여기 없다.
    `pytest` 를 이 프로세스 밖에서 따로 실행하는 것이 자연스럽고, 이 명령은
    "지금 이 프로필로 기동해도 되는가" 만 본다.
    """

    name: ClassVar[str] = "preflight"
    help: ClassVar[str] = "기동 전 점검을 실행한다"

    def add_command_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--required-prompt",
            action="append",
            default=[],
            help="있어야 하는 프롬프트 이름. 여러 번 줄 수 있다",
        )
        parser.add_argument(
            "--extra-workdir",
            action="append",
            default=[],
            help="작업 자리 점검에 추가로 넣을 하위 디렉터리 이름. 여러 번 줄 수 있다",
        )

    def execute_with_profile(self, profile: Profile, args: argparse.Namespace, stdout: TextIO) -> int:
        checks = (
            WorkdirCheck(extra_dirs=args.extra_workdir),
            EngineBinaryCheck(),
            McpServerCheck(),
            PromptFileCheck(required_names=args.required_prompt),
            # 채널 설정 중 소유자 요청에 적용되지 않는 항목을 알린다.
            # fatal=False 라 기동을 막지 않는다 — 설정 오독만 막는 것이 목적이다.
            OwnerSettingsInertCheck(),
        )
        runner = PreflightRunner(checks)
        report = runner.run_all(PreflightContext(profile=profile))

        for check, result in zip(checks, report.results, strict=True):
            if result.ok:
                mark = "통과"
            elif result.fatal:
                mark = "실패"
            else:
                mark = "경고"
            print(f"[{mark}] {check.name} : {result.detail}", file=stdout)

        print("기동 가능" if report.bootable else "기동 불가", file=stdout)
        return 0 if report.bootable else 1


class MigrateCommand(ProfileAwareCommand):
    """DB 스키마를 마이그레이션한다. 원본은 봇이 기동할 때마다 알아서 했다.

    이 명령은 기동과 분리해, 기동 전에 미리 적용했는지 확인할 수 있게 한다.
    """

    name: ClassVar[str] = "migrate"
    help: ClassVar[str] = "DB 마이그레이션을 실행한다"

    def add_command_arguments(self, parser: argparse.ArgumentParser) -> None:
        return

    def execute_with_profile(self, profile: Profile, args: argparse.Namespace, stdout: TextIO) -> int:
        db = Database(profile.paths.database)
        try:
            before = int(db.connect().execute("PRAGMA user_version").fetchone()[0])
            after = db.migrate()
            if before == after:
                print(f"이미 최신입니다 (버전 {after})", file=stdout)
            else:
                print(f"마이그레이션을 적용했습니다 (버전 {before} -> {after})", file=stdout)
        finally:
            db.close()
        return 0


class ChannelsCommand(ProfileAwareCommand):
    """등록된 채널 목록을 본다. 원본은 채널 목록을 보려면 파일을 직접 열어야 했다."""

    name: ClassVar[str] = "channels"
    help: ClassVar[str] = "등록된 채널 목록을 본다"

    def add_command_arguments(self, parser: argparse.ArgumentParser) -> None:
        return

    def execute_with_profile(self, profile: Profile, args: argparse.Namespace, stdout: TextIO) -> int:
        registry = ChannelRegistry(profile.paths.channels)
        channels = registry.all()
        if not channels:
            print("등록된 채널이 없다", file=stdout)
            return 0
        for channel_id, config in channels.items():
            print(f"{channel_id} : {config.name} (모드 {config.mode})", file=stdout)
        return 0


ApplicationFactory = Callable[[Profile], Any]
"""프로필을 받아 애플리케이션 객체를 돌려주는 함수의 타입.

애플리케이션 객체가 갖춰야 하는 계약은 `IngressCommand`, `WorkerCommand` 가
쓰는 메서드(`ingress()`, `gateway()`, `worker(worker_id=...)`, `channel_ids()`,
`close()`)뿐이다. 그 구현이 무엇인지는 이 파일이 알 필요가 없다.
"""


def _default_application(profile: Profile) -> Any:
    from .core.application import Application  # 지연 import. 슬랙 SDK 는 여기서만 끌려온다

    return Application.from_profile(profile)


class IngressCommand(ProfileAwareCommand):
    """슬랙 이벤트 접수 프로세스를 띄운다. 원본 `run.sh`/`restart.sh` 뒷부분에 해당한다."""

    name: ClassVar[str] = "ingress"
    help: ClassVar[str] = "슬랙 이벤트 접수 프로세스를 띄운다"

    def __init__(self, application_factory: ApplicationFactory | None = None) -> None:
        self._factory = application_factory or _default_application

    def add_command_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--app-token",
            default=None,
            help="슬랙 앱 토큰. 없으면 환경변수 SLACK_APP_TOKEN 을 쓴다",
        )

    def execute_with_profile(self, profile: Profile, args: argparse.Namespace, stdout: TextIO) -> int:
        token = args.app_token or os.environ.get("SLACK_APP_TOKEN")
        if not token:
            print(
                "앱 토큰이 없다. --app-token 인자나 환경변수 SLACK_APP_TOKEN 을 채워야 기동한다",
                file=stdout,
            )
            return 2

        app = self._factory(profile)
        try:
            gateway = app.gateway()
            app.ingress().register(gateway)
            # 연결 감시는 기동 전에 켠다. 소켓 연결은 이 프로세스에만 있으므로
            # 여기서 안 켜면 어디서도 안 켜지고, 소켓이 끊겨도 아무 기록이 안 남는다.
            app.connection_watch()
            # 주기 실행기는 묶음으로 띄운다. 여기서 하나씩 손으로 시작하면
            # 새 실행기를 추가할 때 이 위치를 같이 안 고쳐 그 동작이 실행되지 않는다.
            with app.ingress_services(app.self_restarter()):
                gateway.start(token)
        finally:
            app.close()
        return 0


class WorkerCommand(ProfileAwareCommand):
    """작업 큐를 소비하는 워커 프로세스를 띄운다. 원본 `run.sh`/`restart.sh` 뒷부분에 해당한다."""

    name: ClassVar[str] = "worker"
    help: ClassVar[str] = "작업 큐를 소비하는 워커 프로세스를 띄운다"

    def __init__(
        self,
        application_factory: ApplicationFactory | None = None,
        signal_register: SignalRegister = signal.signal,
    ) -> None:
        self._factory = application_factory or _default_application
        # 신호 등록을 주입받는다. `signal.signal` 을 그대로 부르면 단위
        # 시험이 실제 프로세스의 신호 처리기를 바꾼다.
        self._signal_register = signal_register

    def add_command_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--worker-id", default="worker", help="이 워커를 구분할 이름")
        parser.add_argument("--once", action="store_true", help="한 번만 처리하고 끝난다")
        parser.add_argument(
            "--no-catch-up",
            dest="catch_up",
            action="store_false",
            help="시작 시 되짚기를 건너뛴다. 기본은 되짚는다",
        )
        parser.set_defaults(catch_up=True)

    def execute_with_profile(self, profile: Profile, args: argparse.Namespace, stdout: TextIO) -> int:
        app = self._factory(profile)
        worker = app.worker(worker_id=args.worker_id)
        # 종료 신호를 받으면 처리 중인 요청이 끝날 때까지 기다린다. 등록하지
        # 않으면 SIGTERM 이 프로세스를 그 자리에서 끝내고, 집어 둔 작업이
        # 큐에 잡힌 채 남아 다음 워커가 정체 판정 시각까지 못 집는다.
        shutdown = GracefulShutdown(
            worker.inflight,
            app.settings.shutdown_grace_sec,
            signal_register=self._signal_register,
            # 종료 중이라는 사실을 상태 기록에 남긴다. 없으면 상태 파일만
            # 보는 쪽이 멈춘 프로세스와 종료 중인 프로세스를 구분하지 못한다.
            on_shutdown_start=lambda inflight: app.mark_shutting_down(),
        )
        shutdown.register()
        try:
            # 주기 실행기는 묶음으로 띄운다. 상태 기록 갱신, 감시 확인, 끝난
            # 작업 정리가 여기 들어 있다. 하나씩 손으로 시작하던 때에 실제로
            # 추가한 실행기를 이 위치에서 빠뜨린 적이 있다.
            with app.worker_services(worker):
                worker.reclaim()
                # 되짚기는 기본으로 한다. 재기동 중에 들어온 멘션은 소켓
                # 이벤트로 다시 오지 않으므로, 플래그를 줘야만 되짚으면 그것을
                # 빠뜨린 순간 그 요청들이 유실된다.
                if args.catch_up:
                    worker.catch_up(app.channel_ids())
                try:
                    if args.once:
                        worker.run_once()
                    else:
                        # 반복은 워커가 맡는다. 여기서 run_once 를 되풀이하면 큐가
                        # 비었을 때의 대기가 빠져 SQLite 조회가 쉬지 않고 일어난다.
                        worker.run_forever(lambda: shutdown.is_shutting_down)
                except KeyboardInterrupt:
                    pass
        finally:
            worker.shutdown()
            app.close()
        return 0


class LearnCommand(ProfileAwareCommand):
    """하루치 학습 배치를 한 번 실행한다. 원본 `run-learn.sh` 에 대응한다.

    워커 주기 실행기가 부르는 것과 같은 배치를 부른다. 손으로 실행하는 쪽이
    다른 코드를 쓰면 두 경로의 동작이 갈린다.
    """

    name: ClassVar[str] = "learn"
    help: ClassVar[str] = "하루치 응답 기록을 분석해 학습 제안을 만들고 반영한다"

    def __init__(self, application_factory: ApplicationFactory | None = None) -> None:
        self._factory = application_factory or _default_application

    def add_command_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--day",
            default=None,
            help="분석할 날짜(YYYY-MM-DD). 안 주면 오늘을 분석한다",
        )

    def execute_with_profile(self, profile: Profile, args: argparse.Namespace, stdout: TextIO) -> int:
        app = self._factory(profile)
        try:
            report = app.learning_batch().run(args.day)
        finally:
            app.close()
        if not report.ran:
            print(f"{report.day} 학습 배치를 실행하지 않았다 : {report.reason}", file=stdout)
            return 1
        where = ", ".join(f"{k} {v}건" for k, v in report.applied.items()) or "없음"
        print(f"{report.day} 학습 배치를 마쳤다. 반영 : {where}", file=stdout)
        if not report.notified:
            print("소유자에게 알리지 못했다. 반영은 끝났다.", file=stdout)
        return 0


DEFAULT_COMMANDS: tuple[CliCommand, ...] = (
    PreflightCommand(),
    MigrateCommand(),
    ChannelsCommand(),
    IngressCommand(),
    WorkerCommand(),
    LearnCommand(),
)


class SlackCliAgent:
    """하위 명령을 모아 하나의 argparse 진입점으로 만든다."""

    def __init__(self, commands: Sequence[CliCommand] | None = None) -> None:
        self._commands = {c.name: c for c in (commands or DEFAULT_COMMANDS)}

    def command_names(self) -> list[str]:
        return list(self._commands)

    def build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            prog="slack-cli-agent", description="로컬 CLI 에이전트를 슬랙에 연결하는 범용 어댑터"
        )
        subparsers = parser.add_subparsers(dest="command", required=True)
        for command in self._commands.values():
            sub = subparsers.add_parser(command.name, help=command.help)
            command.add_arguments(sub)
        return parser

    def run(self, argv: Sequence[str] | None = None, stdout: TextIO = sys.stdout) -> int:
        parser = self.build_parser()
        args = parser.parse_args(argv)
        command = self._commands[args.command]
        try:
            return command.execute(args, stdout)
        except AgentError as exc:
            print(f"오류 : {exc}", file=stdout)
            return 2


def main(argv: Sequence[str] | None = None) -> int:
    return SlackCliAgent().run(argv)


if __name__ == "__main__":
    sys.exit(main())
