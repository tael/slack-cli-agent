"""상시 기동 명령이 기동 전에 통과해야 하는 게이트.

설정 오류로 못 뜨는 봇이 조용히 실패하는 대신, 점검 결과를 남기고 78
(EX_CONFIG)로 끝난다. run.sh 가 그 코드를 보고 차단 표식을 남기고, plist 의
PathState 가 재기동을 멈춘다 (sca-xay, sca-y4q).

부작용의 부재로 "기동하지 않았다" 를 보면 검출력이 0 이 된다. 기동 경계의
호출 횟수를 직접 센다.
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
from typing import TextIO

import pytest
from preflight_support import 고정Suite

from slack_cli_agent.cli import (
    BLOCKED_EXIT,
    IngressCommand,
    PreflightGatedServiceCommand,
    SlackCliAgent,
    WorkerCommand,
)
from slack_cli_agent.config.profile import Profile
from slack_cli_agent.core.errors import SlackError

REPO = Path(__file__).resolve().parents[2]


class 기록하는명령(PreflightGatedServiceCommand):
    name = "기록"

    def __init__(self, *, bootable: bool, 반환: int = 0) -> None:
        super().__init__(preflight_suite_factory=lambda: 고정Suite(bootable=bootable))
        self.호출 = 0
        self._반환 = 반환

    def add_command_arguments(self, parser: argparse.ArgumentParser) -> None:
        pass

    def run_service(self, profile: Profile, args: argparse.Namespace, stdout: TextIO) -> int:
        self.호출 += 1
        return self._반환


def _프로필(tmp_path: Path) -> Path:
    profiles = tmp_path / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    (profiles / "example.json").write_text(
        json.dumps(
            {
                "name": "example",
                "primary_engine": {"type": "claude", "binary": "python3", "model": "m"},
                "owner_user_id": "U1",
                "troubleshoot_channel": "C1",
                "state_dir": str(tmp_path / "state"),
            }
        ),
        encoding="utf-8",
    )
    return profiles


def _돌린다(command: 기록하는명령, profiles: Path) -> tuple[int, str]:
    out = io.StringIO()
    code = SlackCliAgent([command]).run(
        ["기록", "--profile", "example", "--profile-dir", str(profiles)], stdout=out
    )
    return code, out.getvalue()


class Test점검이_막으면:
    def test_78_로_끝난다(self, tmp_path: Path) -> None:
        code, _ = _돌린다(기록하는명령(bootable=False), _프로필(tmp_path))
        assert code == BLOCKED_EXIT == 78

    def test_본체를_한_번도_부르지_않는다(self, tmp_path: Path) -> None:
        명령 = 기록하는명령(bootable=False)
        _돌린다(명령, _프로필(tmp_path))
        assert 명령.호출 == 0

    def test_왜_막혔는지_출력한다(self, tmp_path: Path) -> None:
        """차단만 하고 사유를 안 내면 로그에 아무것도 안 남아 원인을 못 찾는다."""
        _, 나온것 = _돌린다(기록하는명령(bootable=False), _프로필(tmp_path))
        assert "기동 불가" in 나온것
        assert "고정" in 나온것


class Test점검을_통과하면:
    def test_본체를_한_번_부른다(self, tmp_path: Path) -> None:
        명령 = 기록하는명령(bootable=True)
        code, _ = _돌린다(명령, _프로필(tmp_path))
        assert 명령.호출 == 1
        assert code == 0

    def test_본체의_종료코드를_그대로_돌려준다(self, tmp_path: Path) -> None:
        """게이트가 0 으로 덮으면 토큰 없음(2) 같은 기동 실패가 성공으로 읽힌다."""
        code, _ = _돌린다(기록하는명령(bootable=True, 반환=2), _프로필(tmp_path))
        assert code == 2

    def test_통과도_출력한다(self, tmp_path: Path) -> None:
        """성공 경로가 조용하면 실패 로그 하나가 그 뒤 구간 전체로 읽힌다."""
        _, 나온것 = _돌린다(기록하는명령(bootable=True), _프로필(tmp_path))
        assert "기동 가능" in 나온것


class Test상시_기동_명령이_게이트를_거친다:
    """게이트를 만든 것과 그것을 기동 경로에 연결한 것은 다르다."""

    @pytest.mark.parametrize("클래스", [WorkerCommand, IngressCommand])
    def test_게이트_기반_클래스를_상속한다(self, 클래스: type) -> None:
        assert issubclass(클래스, PreflightGatedServiceCommand)

    @pytest.mark.parametrize("이름", ["worker", "ingress"])
    def test_점검이_막으면_application_을_만들지_않는다(self, tmp_path: Path, 이름: str) -> None:
        만든횟수 = 0

        def factory(profile, resolver):
            nonlocal 만든횟수
            만든횟수 += 1
            raise AssertionError("기동이 막혔는데 application 을 만들었다")

        클래스 = WorkerCommand if 이름 == "worker" else IngressCommand
        명령 = 클래스(
            application_factory=factory,
            preflight_suite_factory=lambda: 고정Suite(bootable=False),
        )
        out = io.StringIO()
        code = SlackCliAgent([명령]).run(
            [이름, "--profile", "example", "--profile-dir", str(_프로필(tmp_path))], stdout=out
        )
        assert code == BLOCKED_EXIT
        assert 만든횟수 == 0


class Test종료코드가_wrapper_와_같다:
    def test_템플릿의_BLOCKED_EXIT_와_일치한다(self) -> None:
        """두 자리에 따로 박힌 78 은 한쪽만 고치면 조용히 어긋난다.
        어긋나면 게이트가 막아도 표식이 안 생겨 재기동이 계속된다."""
        본문 = (REPO / "tools" / "templates" / "run.sh").read_text(encoding="utf-8")
        줄 = [line for line in 본문.splitlines() if line.startswith("BLOCKED_EXIT=")]
        assert 줄 == [f"BLOCKED_EXIT={BLOCKED_EXIT}"], 줄


class Test점검과_토큰이_같이_없으면:
    """게이트가 토큰 검사보다 먼저 돈다. 둘 다 잘못됐을 때 어느 쪽을 내는지는
    정책이므로 시험에 적어 둔다 - 78 은 차단 표식을 남기고 2 는 남기지 않아
    복구 절차가 달라진다 (코덱스 리뷰).
    """

    def _프로필_토큰없이(self, tmp_path: Path) -> Path:
        return _프로필(tmp_path)

    def test_설정_오류를_먼저_낸다(self, tmp_path: Path) -> None:
        명령 = IngressCommand(
            application_factory=lambda profile, resolver: pytest.fail("기동이 막혀야 한다"),
            preflight_suite_factory=lambda: 고정Suite(bootable=False),
        )
        out = io.StringIO()
        code = SlackCliAgent([명령]).run(
            ["ingress", "--profile", "example", "--profile-dir", str(_프로필(tmp_path))],
            stdout=out,
        )
        assert code == BLOCKED_EXIT
        # 토큰 안내가 아니라 점검 결과가 나와야 한다. 둘을 같이 내면 어느
        # 것을 먼저 고쳐야 하는지가 흐려진다.
        assert "기동 불가" in out.getvalue()
        assert "앱 토큰이 없다" not in out.getvalue()

    def test_점검을_통과해도_토큰_누락은_차단이다(self, tmp_path: Path) -> None:
        """2026-09-17 에 뒤집었다(sca-q2k).

        앞서는 토큰 누락을 재기동으로 풀릴 수 있는 것으로 보고 2 로 뒀다.
        토큰은 사람이 파일에 쓰기 전에는 저절로 생기지 않으므로 그 사이
        launchd 가 무한히 재기동한다. 표식을 지우는 손이 한 번 더 드는 대신
        재기동이 멈추고 사유가 파일로 남는다 - tools/unblock.sh 로 지운다."""
        명령 = IngressCommand(
            application_factory=lambda profile, resolver: pytest.fail("토큰 없이 기동했다"),
            preflight_suite_factory=lambda: 고정Suite(bootable=True),
        )
        out = io.StringIO()
        code = SlackCliAgent([명령]).run(
            ["ingress", "--profile", "example", "--profile-dir", str(_프로필(tmp_path))],
            stdout=out,
        )
        assert code == BLOCKED_EXIT
        assert "앱 토큰이 없다" in out.getvalue()


class Test실제_점검으로도_막는다:
    """위 시험들은 전부 대역 suite 를 주입한다. 그러면 게이트가 진짜 검사
    목록과 연결됐는지는 아무도 안 본다 - 기본값을 PreflightSuite 가 아닌
    것으로 바꿔도 통과한다.
    """

    def test_기본_suite_로_돌면_작업_자리_없음이_막는다(self, tmp_path: Path) -> None:
        profiles = _프로필(tmp_path)  # work_root 를 안 만든다
        명령 = WorkerCommand(
            application_factory=lambda profile, resolver: pytest.fail("기동이 막혀야 한다")
        )
        out = io.StringIO()
        code = SlackCliAgent([명령]).run(
            ["worker", "--profile", "example", "--profile-dir", str(profiles)], stdout=out
        )
        assert code == BLOCKED_EXIT
        assert "workdir" in out.getvalue()


class Test결과가_로그에_실제로_닿는다:
    """launchd 아래에서 stdout 은 파일이라 블록 버퍼링이다. 프로세스가 계속
    살아 있으면 버퍼가 안 비워져, 통과한 경우 점검 결과가 로그에 영영 안
    남는다. 2026-09-17 에 실제로 out.log 가 0바이트였다.
    """

    class 기록하는출력(io.StringIO):
        def __init__(self) -> None:
            super().__init__()
            self.flush_횟수 = 0

        def flush(self) -> None:
            self.flush_횟수 += 1
            super().flush()

    def _돌린다(self, tmp_path: Path, *, bootable: bool) -> 기록하는출력:
        out = self.기록하는출력()
        SlackCliAgent([기록하는명령(bootable=bootable)]).run(
            ["기록", "--profile", "example", "--profile-dir", str(_프로필(tmp_path))], stdout=out
        )
        return out

    def test_통과해도_비운다(self, tmp_path: Path) -> None:
        assert self._돌린다(tmp_path, bootable=True).flush_횟수 >= 1

    def test_막을_때도_비운다(self, tmp_path: Path) -> None:
        """차단은 곧 프로세스 종료라 대개 비워지지만, 종료 경로가 바뀌어도
        사유가 남아야 한다."""
        assert self._돌린다(tmp_path, bootable=False).flush_횟수 >= 1


class Test설정_오류는_차단_코드로_끝낸다:
    """설정 오류는 재기동으로 안 풀린다. 78 이 아니면 표식이 안 생겨 무한 재기동이다(sca-q2k).

    2026-09-17 빈 설치 실측 - 앱 토큰 없이 ingress 는 2, model 이 빈 프로필로
    preflight 도 2 였다. 둘 다 사람이 파일을 고쳐야 풀리는 상태다.
    """

    def test_프로필을_못_찾으면_차단_코드다(self, tmp_path: Path) -> None:
        out = io.StringIO()
        code = SlackCliAgent().run(
            ["preflight", "--profile", "없는프로필", "--profile-dir", str(tmp_path)], stdout=out
        )
        assert code == BLOCKED_EXIT
        assert "없는프로필" in out.getvalue()

    def test_프로필_값이_빠져도_차단_코드다(self, tmp_path: Path) -> None:
        (tmp_path / "봇.json").write_text(
            json.dumps({"name": "봇", "primary_engine": {"type": "claude", "binary": "/bin/echo"}}),
            encoding="utf-8",
        )
        out = io.StringIO()
        code = SlackCliAgent().run(
            ["preflight", "--profile", "봇", "--profile-dir", str(tmp_path)], stdout=out
        )
        assert code == BLOCKED_EXIT

    def test_설정이_아닌_실패는_그대로_2다(self, tmp_path: Path) -> None:
        """슬랙이 잠깐 안 되는 것은 재기동으로 풀린다. 그것까지 차단하면 봇이 안 뜬다."""

        class 슬랙이막힌명령(PreflightGatedServiceCommand):
            name = "슬랙막힘"

            def __init__(self) -> None:
                super().__init__(preflight_suite_factory=lambda: 고정Suite(bootable=True))

            def add_command_arguments(self, parser: argparse.ArgumentParser) -> None:
                pass

            def run_service(
                self, profile: Profile, args: argparse.Namespace, stdout: TextIO
            ) -> int:
                raise SlackError("슬랙 응답 없음")

        profiles = tmp_path / "profiles"
        profiles.mkdir(parents=True)
        (profiles / "example.json").write_text(
            json.dumps(
                {
                    "name": "example",
                    "state_dir": str(tmp_path / "state"),
                    "primary_engine": {"type": "claude", "binary": "/bin/echo", "model": "m"},
                }
            ),
            encoding="utf-8",
        )
        code = SlackCliAgent([슬랙이막힌명령()]).run(
            ["슬랙막힘", "--profile", "example", "--profile-dir", str(profiles)],
            stdout=io.StringIO(),
        )
        assert code == 2


class Test자격은_게이트에서_확인한다:
    """자격이 없으면 기동 전에 막는다(sca-q2k).

    워커는 봇 토큰이 없어도 조립까지 지나가고 첫 슬랙 호출에서야 실패했다.
    그 실패는 요청마다 나므로 launchd 는 정상 기동으로 보고 재기동하지 않는다 -
    봇은 떠 있는데 아무 일도 못 하는 상태가 이어진다.
    """

    def _프로필(self, tmp_path: Path) -> Path:
        profiles = tmp_path / "profiles"
        profiles.mkdir(parents=True)
        (profiles / "example.json").write_text(
            json.dumps(
                {
                    "name": "example",
                    "state_dir": str(tmp_path / "state"),
                    "primary_engine": {"type": "claude", "binary": "/bin/echo", "model": "m"},
                }
            ),
            encoding="utf-8",
        )
        return profiles

    def test_워커는_봇_토큰이_없으면_차단_코드로_끝낸다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        불렀나: list[str] = []
        명령 = WorkerCommand(
            application_factory=lambda profile, resolver: 불렀나.append("조립"),
            preflight_suite_factory=lambda: 고정Suite(bootable=True),
            signal_register=lambda *args: None,
        )
        out = io.StringIO()
        code = SlackCliAgent([명령]).run(
            ["worker", "--profile", "example", "--profile-dir", str(self._프로필(tmp_path))],
            stdout=out,
        )
        assert code == BLOCKED_EXIT, out.getvalue()
        assert 불렀나 == [], "토큰이 없으면 조립까지 가면 안 된다"
        assert "봇 토큰" in out.getvalue()

    def test_접수기는_앱_토큰이_없으면_차단_코드로_끝낸다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-있음")
        불렀나: list[str] = []
        명령 = IngressCommand(
            application_factory=lambda profile, resolver: 불렀나.append("조립"),
            preflight_suite_factory=lambda: 고정Suite(bootable=True),
        )
        out = io.StringIO()
        code = SlackCliAgent([명령]).run(
            ["ingress", "--profile", "example", "--profile-dir", str(self._프로필(tmp_path))],
            stdout=out,
        )
        assert code == BLOCKED_EXIT, out.getvalue()
        assert 불렀나 == []

    def test_자격이_다_있으면_조립까지_간다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """막는 것만 시험하면 항상 막는 구현도 통과한다."""
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-있음")
        조립했다: list[str] = []

        def factory(profile: Profile, resolver: object) -> object:
            조립했다.append("조립")
            raise KeyboardInterrupt

        명령 = WorkerCommand(
            application_factory=factory,
            preflight_suite_factory=lambda: 고정Suite(bootable=True),
            signal_register=lambda *args: None,
        )
        with pytest.raises(KeyboardInterrupt):
            SlackCliAgent([명령]).run(
                [
                    "worker", "--profile", "example",
                    "--profile-dir", str(self._프로필(tmp_path)),
                ],
                stdout=io.StringIO(),
            )
        assert 조립했다 == ["조립"]
