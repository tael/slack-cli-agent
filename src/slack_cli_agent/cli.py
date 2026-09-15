"""Single entry point CLI.

Subcommands are classes sharing the `CliCommand` contract instead of an
if/elif dispatch. Commands that spawn long-running processes take an
application factory; the default factory imports `core.application` lazily
so importing this module alone doesn't pull in the Slack SDK, which would
otherwise block subcommands (like preflight) that don't need it.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import webbrowser
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar, Protocol, TextIO, runtime_checkable

from .config.channel import ChannelRegistry
from .config.paths import StatePaths, default_profile_dirs, default_profile_write_dir
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
    name: ClassVar[str]
    help: ClassVar[str] = ""

    @abstractmethod
    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        pass

    @abstractmethod
    def execute(self, args: argparse.Namespace, stdout: TextIO) -> int:
        pass


def _search_dirs(given: list[str] | None) -> list[Path]:
    """One resolution for every command that takes --profile-dir (sca-jl4.3)."""
    if given:
        return [Path(p).expanduser() for p in given]
    return list(default_profile_dirs())


class ProfileAwareCommand(CliCommand):
    """Shared `--profile`/`--profile-dir` handling.

    Handled once here so concrete commands only ever deal with an
    already-loaded `Profile`.
    """

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--profile", required=True, help="프로필 이름")
        parser.add_argument(
            "--profile-dir",
            action="append",
            default=None,
            help=(
                "프로필 파일(<이름>.json)을 찾을 디렉터리. 여러 번 줄 수 있다. "
                "기본은 $SLACK_CLI_AGENT_PROFILE_DIR, 사용자 설정 디렉터리, 현재 디렉터리 순"
            ),
        )
        self.add_command_arguments(parser)

    @abstractmethod
    def add_command_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Command-specific arguments; leave the body empty if there are none."""

    def execute(self, args: argparse.Namespace, stdout: TextIO) -> int:
        search_dirs = _search_dirs(args.profile_dir)
        profile = Profile.load(args.profile, search_dirs)
        return self.execute_with_profile(profile, args, stdout)

    @abstractmethod
    def execute_with_profile(self, profile: Profile, args: argparse.Namespace, stdout: TextIO) -> int:
        pass


class PreflightCommand(ProfileAwareCommand):
    """Runs pre-boot checks.

    Syntax checks and unit tests run outside this process via `pytest`;
    this only answers whether the profile can boot right now.
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
            # fatal=False: only meant to prevent a misread config, not to block boot.
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
    """Applies DB schema migrations, decoupled from boot so they can be applied ahead of time."""

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


def _profile_skeleton(name: str, state_dir: Path) -> dict[str, Any]:
    return {
        "name": name,
        "display_name": name,
        "primary_engine": {
            "type": "claude",
            "binary": "~/.local/bin/claude",
            "model": "",
        },
        "state_dir": str(state_dir),
        "owner_user_id": "",
        "troubleshoot_channel": "",
        "plugins": [],
    }


class InitCommand(CliCommand):
    """First-run scaffolding: a profile skeleton plus the state directory layout.

    Never overwrites an existing file or directory's contents — running this
    again after hand edits, or after a package upgrade, must be a no-op on
    anything already there.
    """

    name: ClassVar[str] = "init"
    help: ClassVar[str] = "프로필 뼈대와 상태 디렉터리 구조를 만든다"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--name", required=True, help="봇 이름. 프로필 파일 이름과 상태 디렉터리 기본값에 쓰인다")
        parser.add_argument(
            "--profile-dir",
            default=None,
            help=(
                "프로필 파일(<이름>.json)을 만들 디렉터리. "
                "기본은 $SLACK_CLI_AGENT_PROFILE_DIR 의 첫 경로, 없으면 사용자 설정 디렉터리"
            ),
        )
        parser.add_argument(
            "--state-dir",
            default=None,
            help="상태 디렉터리 경로. 기본은 ~/.<이름>",
        )

    def execute(self, args: argparse.Namespace, stdout: TextIO) -> int:
        name = args.name
        profile_dir = (
            Path(args.profile_dir).expanduser() if args.profile_dir else default_profile_write_dir()
        )
        state_dir = (
            Path(args.state_dir).expanduser() if args.state_dir else StatePaths.for_bot(name).root
        )

        created: list[str] = []
        skipped: list[str] = []

        profile_dir.mkdir(parents=True, exist_ok=True)
        profile_path = profile_dir / f"{name}.json"
        if profile_path.exists():
            skipped.append(f"프로필 파일 : {profile_path}")
        else:
            skeleton = json.dumps(_profile_skeleton(name, state_dir), ensure_ascii=False, indent=2)
            profile_path.write_text(skeleton + "\n", encoding="utf-8")
            created.append(f"프로필 파일 : {profile_path}")

        paths = StatePaths(state_dir)
        for label, path in (
            ("상태 디렉터리", paths.root),
            ("프롬프트 디렉터리", paths.prompts),
            ("페르소나 디렉터리", paths.persona),
            ("축적 지식 디렉터리", paths.knowledge),
            ("엔진 디렉터리", paths.engine_dir),
        ):
            if path.is_dir():
                skipped.append(f"{label} : {path}")
            else:
                path.mkdir(parents=True, exist_ok=True)
                created.append(f"{label} : {path}")

        print("만든 것", file=stdout)
        for line in created:
            print(f"- {line}", file=stdout)
        if not created:
            print("- 없음. 전부 이미 있었다", file=stdout)

        print("건너뛰었다 (이미 있어서 그대로 두었다)", file=stdout)
        for line in skipped:
            print(f"- {line}", file=stdout)
        if not skipped:
            print("- 없음", file=stdout)

        print(file=stdout)
        print("다음에 채워야 할 값", file=stdout)
        print(f"- {profile_path} 의 owner_user_id, troubleshoot_channel, primary_engine", file=stdout)
        print("- 환경변수 SLACK_BOT_TOKEN, SLACK_APP_TOKEN", file=stdout)
        print(f"- {paths.prompts} 아래 조직 고유 프롬프트. 없으면 패키지 기본 프롬프트를 쓴다", file=stdout)
        return 0


ApplicationFactory = Callable[[Profile], Any]
"""Produces an application object exposing `ingress()`, `gateway()`,
`worker(worker_id=...)`, `channel_ids()`, and `close()`."""


def _default_application(profile: Profile) -> Any:
    from .core.application import Application  # lazy import; the Slack SDK only gets pulled in here

    return Application.from_profile(profile)


class IngressCommand(ProfileAwareCommand):
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
            # Must be enabled before start: the socket connection only exists in
            # this process, so a drop would otherwise go unnoticed.
            app.connection_watch()
            # Periodic runners start as a bundle, so adding a new one can't be
            # forgotten at a call site that starts them one by one.
            with app.ingress_services(app.self_restarter()):
                gateway.start(token)
        finally:
            app.close()
        return 0


class WorkerCommand(ProfileAwareCommand):
    name: ClassVar[str] = "worker"
    help: ClassVar[str] = "작업 큐를 소비하는 워커 프로세스를 띄운다"

    def __init__(
        self,
        application_factory: ApplicationFactory | None = None,
        signal_register: SignalRegister = signal.signal,
    ) -> None:
        self._factory = application_factory or _default_application
        # Injected so tests don't install a handler on the real process's signals.
        self._signal_register = signal_register

    def add_command_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--worker-id", default="worker", help="이 워커를 구분할 이름")
        parser.add_argument("--once", action="store_true", help="한 번만 처리하고 끝난다")
        parser.add_argument(
            "--no-catch-up",
            dest="catch_up",
            action="store_false",
            help="시작 시 캐치업을 건너뛴다. 기본은 캐치업한다",
        )
        parser.set_defaults(catch_up=True)

    def execute_with_profile(self, profile: Profile, args: argparse.Namespace, stdout: TextIO) -> int:
        app = self._factory(profile)
        worker = app.worker(worker_id=args.worker_id)
        # Waits for in-flight work on shutdown; without this, SIGTERM kills the
        # process mid-request and the job stays stuck in the queue until the
        # next worker's stall timeout.
        shutdown = GracefulShutdown(
            worker.inflight,
            app.settings.shutdown_grace_sec,
            signal_register=self._signal_register,
            # Recorded so a state-file reader can tell a shutting-down process
            # from a dead one.
            on_shutdown_start=lambda inflight: app.mark_shutting_down(),
        )
        shutdown.register()
        try:
            # Periodic runners (state updates, watch checks, finished-job
            # cleanup) start as a bundle, since adding one by hand here has
            # been forgotten before.
            with app.worker_services(worker):
                worker.reclaim()
                # Catch-up defaults to on: a mention that arrives during a
                # restart doesn't replay as a socket event, so skipping this
                # silently drops it.
                if args.catch_up:
                    worker.catch_up(app.channel_ids())
                try:
                    if args.once:
                        worker.run_once()
                    else:
                        # The worker owns the retry loop; looping run_once()
                        # here would busy-poll SQLite when the queue is empty.
                        worker.run_forever(lambda: shutdown.is_shutting_down)
                except KeyboardInterrupt:
                    pass
        finally:
            worker.shutdown()
            app.close()
        return 0


class LearnCommand(ProfileAwareCommand):
    """Runs the same batch the worker's periodic runner uses, so a manual
    run can't diverge from the scheduled one."""

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


@runtime_checkable
class ServerLike(Protocol):
    """What WebCommand needs from a server.

    Declared here so mypy checks the real server against it: a test double that
    grows a method the real class lacks otherwise passes every test and fails on
    the first real run.
    """

    @property
    def port(self) -> int: ...

    def start(self) -> None: ...

    def serve_forever(self) -> None: ...

    def stop(self) -> None: ...


ConsoleFactory = Callable[[Sequence[Path], int], ServerLike]


def _default_console(search_dirs: Sequence[Path], port: int) -> ServerLike:
    from .web.console import WebConsole

    return WebConsole(search_dirs).server(port=port)


class WebCommand(CliCommand):
    """설정과 지표를 브라우저에서 보는 콘솔을 띄운다.

    One server covers every profile: per-bot servers would force the operator to
    remember which port belongs to which bot.
    """

    name: ClassVar[str] = "web"
    help: ClassVar[str] = "설정과 지표를 보는 웹 콘솔을 띄운다"

    def __init__(self, console_factory: ConsoleFactory | None = None) -> None:
        self._factory = console_factory or _default_console

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--profile-dir",
            action="append",
            default=None,
            help=(
                "프로필 파일(<이름>.json)을 찾을 디렉터리. 여러 번 줄 수 있다. "
                "기본은 $SLACK_CLI_AGENT_PROFILE_DIR, 사용자 설정 디렉터리, 현재 디렉터리 순"
            ),
        )
        parser.add_argument("--port", type=int, default=8787, help="열 포트. 기본 8787")
        parser.add_argument("--open", action="store_true", help="브라우저도 함께 연다")

    def execute(self, args: argparse.Namespace, stdout: TextIO) -> int:
        search_dirs = _search_dirs(args.profile_dir)
        server = self._factory(search_dirs, args.port)
        server.start()
        url = f"http://127.0.0.1:{server.port}/"
        print(f"콘솔을 열었습니다 : {url}", file=stdout)
        print("멈추려면 Ctrl-C 를 누르세요.", file=stdout)
        if args.open:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.stop()
        return 0


DEFAULT_COMMANDS: tuple[CliCommand, ...] = (
    InitCommand(),
    PreflightCommand(),
    MigrateCommand(),
    ChannelsCommand(),
    IngressCommand(),
    WorkerCommand(),
    LearnCommand(),
    WebCommand(),
)


class SlackCliAgent:
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


_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s : %(message)s"
_LOG_LEVEL_ENV = "SLACK_CLI_AGENT_LOG_LEVEL"


def configure_logging(source_env: Mapping[str, str] | None = None) -> None:
    """루트 로거에 stderr 핸들러를 붙이고 수준을 INFO 로 내린다.

    파이썬 기본 수준이 WARNING 이라, 설정이 없으면 log.info 가 전부
    버려진다. 기동 성공 로그가 운영에서 한 줄도 안 나온 원인이었다
    (2026-09-15 실측). launchd 가 stderr 를 파일로 보내므로 핸들러는
    stderr 로 낸다. 이미 핸들러가 있으면 건드리지 않는다 - 시험이나
    상위 프로그램이 잡은 설정을 덮지 않기 위해서다.
    """
    env = os.environ if source_env is None else source_env
    level_name = env.get(_LOG_LEVEL_ENV, "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    if not isinstance(level, int):
        level = logging.INFO
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        root.addHandler(handler)
    root.setLevel(level)


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    return SlackCliAgent().run(argv)


if __name__ == "__main__":
    sys.exit(main())
