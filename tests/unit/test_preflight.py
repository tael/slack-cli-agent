"""기동 전 점검. PreflightCheck, PreflightRunner, 구체 점검 4종.

`engine.mcp_health.broken_mcp_servers` 는 원본 `bot.py` 의 `mcp_ready()` 를 그대로
옮긴 이식 대상이다. 입력·기대 출력은 원본 소스를 읽어 로직을 따라간
것이다 — 실행 파일이 없으면 "실행 파일 없음", 셰뱅 인터프리터를 PATH 에서
못 찾으면 그 이름으로 실패 문구를 낸다.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from slack_cli_agent.config.profile import KNOWN_KEYS as PROFILE_KNOWN_KEYS
from slack_cli_agent.config.profile import Profile
from slack_cli_agent.core.errors import ConfigError
from slack_cli_agent.engine.environment import ClaudeEnvironmentPolicy
from slack_cli_agent.preflight.check import CheckResult, PreflightCheck, PreflightContext
from slack_cli_agent.preflight.checks import (
    ChannelSettingsReadableCheck,
    EngineBinaryCheck,
    EngineHomeCredentialCheck,
    McpCredentialCheck,
    McpServerCheck,
    OwnerSettingsInertCheck,
    ProfilePermissionCheck,
    ProfileUnknownKeysCheck,
    PromptFileCheck,
    ToolAllowlistEnforcementCheck,
    WorkdirCheck,
)
from slack_cli_agent.preflight.runner import PreflightRunner
from slack_cli_agent.preflight.suite import PreflightSuite


def make_profile(tmp_path: Path, **overrides) -> Profile:
    data = {
        "name": "example",
        "primary_engine": {"type": "claude", "binary": str(tmp_path / "claude_bin"), "model": "m"},
        "owner_user_id": "U1",
        "troubleshoot_channel": "C1",
        "state_dir": str(tmp_path / "state"),
    }
    data.update(overrides)
    return Profile.from_dict(data)


def make_executable(path: Path) -> None:
    path.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


# check.py — 계약 자체


class TestCheckResult:
    def test_기본은_fatal이다(self) -> None:
        result = CheckResult(ok=False, detail="문제")
        assert result.fatal is True

    def test_fatal을_False로_주면_경고만이다(self) -> None:
        result = CheckResult(ok=False, detail="경고", fatal=False)
        assert result.fatal is False


class TestPreflightCheckContract:
    def test_ABC라_직접_인스턴스화하지_못한다(self) -> None:
        with pytest.raises(TypeError):
            PreflightCheck()  # type: ignore[abstract]


# PromptFileCheck


class TestPromptFileCheck:
    def test_필요한_이름이_없으면_통과한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        check = PromptFileCheck(required_names=[])
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_파일이_없으면_실패한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        check = PromptFileCheck(required_names=["PERSONA"])
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "PERSONA" in result.detail
        assert result.fatal is True

    def test_파일이_비어있으면_실패한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        profile.paths.prompts.mkdir(parents=True)
        (profile.paths.prompts / "persona.md").write_text("   ", encoding="utf-8")
        check = PromptFileCheck(required_names=["PERSONA"])
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "비어 있다" in result.detail

    def test_파일이_있고_내용이_있으면_통과한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        profile.paths.prompts.mkdir(parents=True)
        (profile.paths.prompts / "persona.md").write_text("안녕", encoding="utf-8")
        check = PromptFileCheck(required_names=["PERSONA"])
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is True


# WorkdirCheck


class TestWorkdirCheck:
    def test_작업_자리가_없으면_실패한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path, work_root=str(tmp_path / "work" / "missing"))
        check = WorkdirCheck()
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "없다" in result.detail

    def test_작업_자리가_홈_안이면_실패한다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        work_root = home / "work"
        work_root.mkdir()
        profile = make_profile(tmp_path, work_root=str(work_root))
        check = WorkdirCheck()
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "홈 안" in result.detail

    def test_이름이_겹치는_형제_자리는_홈_안이_아니다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """문자열 접두로 비교하면 /Users/example-work 가 /Users/example 안으로
        읽힌다. 이 점검은 기동 게이트가 쓰므로 오판이 곧 기동 거부가 된다."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        work_root = tmp_path / "home-work"
        work_root.mkdir()
        profile = make_profile(tmp_path, work_root=str(work_root))
        result = WorkdirCheck().run(PreflightContext(profile=profile))
        assert result.ok is True, result.detail

    def test_CLAUDE_MD가_있으면_실패한다(self, tmp_path: Path) -> None:
        # isolate_home 이 Path.home() 을 tmp_path 로 바꾸므로, "홈 밖" 을
        # 실제로 시험하려면 tmp_path 자체가 아니라 그 바깥에 작업 자리를 둔다.
        work_root = tmp_path.parent / "outside_home_claude_md" / "work"
        work_root.mkdir(parents=True)
        (work_root / "CLAUDE.md").write_text("x", encoding="utf-8")
        profile = make_profile(tmp_path, work_root=str(work_root))
        check = WorkdirCheck()
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "CLAUDE.md" in result.detail

    def test_정상이면_통과한다(self, tmp_path: Path) -> None:
        work_root = tmp_path.parent / "outside_home_normal" / "work"
        work_root.mkdir(parents=True)
        profile = make_profile(tmp_path, work_root=str(work_root))
        check = WorkdirCheck()
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is True


# EngineBinaryCheck


class TestEngineBinaryCheck:
    def test_절대경로_실행파일이_없으면_실패한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        check = EngineBinaryCheck()
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "1차" in result.detail

    def test_절대경로_실행파일이_있으면_통과한다(self, tmp_path: Path) -> None:
        binary = tmp_path / "claude_bin"
        make_executable(binary)
        profile = make_profile(
            tmp_path,
            primary_engine={"type": "claude", "binary": str(binary), "model": "m"},
        )
        check = EngineBinaryCheck()
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_PATH로_찾는_이름이면_which로_확인한다(self, tmp_path: Path) -> None:
        profile = make_profile(
            tmp_path,
            primary_engine={"type": "codex", "binary": "python3", "model": "m"},
        )
        check = EngineBinaryCheck()
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_2차_엔진도_확인한다(self, tmp_path: Path) -> None:
        binary = tmp_path / "claude_bin"
        make_executable(binary)
        profile = make_profile(
            tmp_path,
            primary_engine={"type": "claude", "binary": str(binary), "model": "m"},
            fallback_engine={
                "type": "codex",
                "binary": str(tmp_path / "no_such_binary"),
                "model": "m2",
            },
        )
        check = EngineBinaryCheck()
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "2차" in result.detail


# McpServerCheck — 이식 대상. mcp_ready() 본문 특성화


class TestMcpCredentialCheck:
    """sca-dn4 — 표기가 안 풀리면 MCP 서버는 빈 자격으로 인증 실패만 낸다.
    요청이 들어온 뒤가 아니라 기동 때 잡는다."""

    def _profile(self, tmp_path: Path, value: str) -> Profile:
        return make_profile(
            tmp_path,
            mcp_servers={"깃허브": {"command": "서버", "env": {"GITHUB_TOKEN": value}}},
        )

    def test_풀리면_통과한다(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GH", "값")
        result = McpCredentialCheck().run(PreflightContext(profile=self._profile(tmp_path, "${env:GH}")))
        assert result.ok is True

    def test_안_풀리면_어느_서버인지_말한다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GH", raising=False)
        result = McpCredentialCheck().run(PreflightContext(profile=self._profile(tmp_path, "${env:GH}")))
        assert result.ok is False
        assert "깃허브" in result.detail

    def test_서버가_없으면_볼_것이_없다(self, tmp_path: Path) -> None:
        result = McpCredentialCheck().run(PreflightContext(profile=make_profile(tmp_path)))
        assert result.ok is True


class TestProfilePermissionCheck:
    """sca-8x7 — 프로필에는 MCP 서버 env 처럼 남에게 보이면 안 되는 값이 들어간다.
    토큰 자체는 로드에서 막지만 env 값 전부를 가려낼 수는 없다."""

    def _profile_file(self, tmp_path: Path, mode: int, *, mcp_env: bool = True) -> Profile:
        path = tmp_path / "example.json"
        data = {
            "name": "example",
            "primary_engine": {"type": "claude", "binary": str(tmp_path / "claude_bin"), "model": "m"},
            "owner_user_id": "U1",
            "troubleshoot_channel": "C1",
            "state_dir": str(tmp_path / "state"),
        }
        if mcp_env:
            data["mcp_servers"] = {"어딘가": {"command": "node", "env": {"API_BASE": "https://x"}}}
        path.write_text(json.dumps(data), encoding="utf-8")
        path.chmod(mode)
        return Profile.load("example", [tmp_path])

    def test_소유자_전용이면_통과한다(self, tmp_path: Path) -> None:
        profile = self._profile_file(tmp_path, 0o600)
        result = ProfilePermissionCheck().run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_남이_읽을_수_있으면_경고한다(self, tmp_path: Path) -> None:
        profile = self._profile_file(tmp_path, 0o644)
        result = ProfilePermissionCheck().run(PreflightContext(profile=profile))
        assert result.ok is False
        assert result.fatal is False
        assert "644" in result.detail

    def test_가릴_값이_없으면_권한을_보지_않는다(self, tmp_path: Path) -> None:
        """프로필은 저장소에 든다. 토큰은 로드에서 막히므로 평범한 프로필까지
        경고하면 세 봇 모두 상시 경고가 붙는다."""
        profile = self._profile_file(tmp_path, 0o644, mcp_env=False)
        result = ProfilePermissionCheck().run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_경로를_모르면_판정하지_않는다(self, tmp_path: Path) -> None:
        """from_dict 로 만든 프로필에는 읽어 온 파일이 없다. 그것을 위반으로
        세면 없는 결함이 보고된다."""
        result = ProfilePermissionCheck().run(PreflightContext(profile=make_profile(tmp_path)))
        assert result.ok is True


class TestMcpServerCheck:
    """점검 대상은 프로필이 선언한 MCP 서버다.

    한때 상태 디렉터리의 engine/mcp.json 을 읽었는데 그 파일을 쓰는 코드가
    이 저장소에 없다. 원본은 그 파일을 --mcp-config 로 넘기지만 이식본은
    claude 에 인라인 JSON, codex 에 -c, gemini 에 작업 디렉터리 파일로
    넘긴다. 파일을 읽는 동안 이 점검은 늘 통과였다 (codex 리뷰).
    """

    @staticmethod
    def 서버를_단다(tmp_path: Path, **서버):
        return make_profile(tmp_path, mcp_servers=서버)

    def test_선언된_서버가_없으면_경고만이고_기동을_막지_않는다(self, tmp_path: Path) -> None:
        result = McpServerCheck().run(PreflightContext(profile=make_profile(tmp_path)))
        assert result.ok is True
        assert result.fatal is False

    def test_실행_파일이_없는_서버는_실패한다(self, tmp_path: Path) -> None:
        profile = self.서버를_단다(tmp_path, broken={"command": str(tmp_path / "no_such")})
        result = McpServerCheck().run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "broken" in result.detail
        assert "실행 파일 없음" in result.detail

    def test_셰뱅_인터프리터를_PATH에서_못_찾으면_실패한다(self, tmp_path: Path) -> None:
        script = tmp_path / "server.js"
        script.write_text(
            "#!/usr/bin/env definitely-not-on-path\nconsole.log(1)\n", encoding="utf-8"
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        profile = self.서버를_단다(tmp_path, **{"js-server": {"command": str(script)}})
        result = McpServerCheck().run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "definitely-not-on-path" in result.detail

    def test_원격_서버는_건너뛴다(self, tmp_path: Path) -> None:
        """url 만 있는 서버에는 확인할 실행 파일이 없다."""
        profile = self.서버를_단다(tmp_path, **{"url-only": {"url": "https://example.test"}})
        result = McpServerCheck().run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_꺼진_서버는_건너뛴다(self, tmp_path: Path) -> None:
        """꺼진 서버는 어느 엔진에도 안 붙는다. 그것을 결함으로 세면 고칠
        수 없는 실패가 기동을 막는다."""
        profile = self.서버를_단다(
            tmp_path, off={"command": str(tmp_path / "no_such"), "disabled": True}
        )
        result = McpServerCheck().run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_정상_서버는_통과한다(self, tmp_path: Path) -> None:
        binary = tmp_path / "ok_server"
        make_executable(binary)
        profile = self.서버를_단다(tmp_path, ok={"command": str(binary)})
        result = McpServerCheck().run(PreflightContext(profile=profile))
        assert result.ok is True


# PreflightRunner


class _AlwaysOk(PreflightCheck):
    name = "always_ok"

    def run(self, ctx: PreflightContext) -> CheckResult:
        return CheckResult(ok=True, detail="ok")


class _AlwaysFatalFail(PreflightCheck):
    name = "always_fatal_fail"

    def run(self, ctx: PreflightContext) -> CheckResult:
        return CheckResult(ok=False, detail="fatal 실패")


class _AlwaysWarn(PreflightCheck):
    name = "always_warn"

    def run(self, ctx: PreflightContext) -> CheckResult:
        return CheckResult(ok=False, detail="경고만", fatal=False)


class TestPreflightRunner:
    def test_전부_통과하면_기동_가능하다(self, tmp_path: Path) -> None:
        runner = PreflightRunner([_AlwaysOk(), _AlwaysOk()])
        report = runner.run_all(PreflightContext(profile=make_profile(tmp_path)))
        assert report.bootable is True
        assert report.failures() == ()

    def test_fatal_실패가_있으면_기동_불가다(self, tmp_path: Path) -> None:
        runner = PreflightRunner([_AlwaysOk(), _AlwaysFatalFail()])
        report = runner.run_all(PreflightContext(profile=make_profile(tmp_path)))
        assert report.bootable is False
        assert len(report.fatal_failures()) == 1

    def test_경고만_있으면_기동_가능하다(self, tmp_path: Path) -> None:
        runner = PreflightRunner([_AlwaysOk(), _AlwaysWarn()])
        report = runner.run_all(PreflightContext(profile=make_profile(tmp_path)))
        assert report.bootable is True
        assert len(report.failures()) == 1

    def test_첫_실패에서_멈추지_않고_전부_실행한다(self, tmp_path: Path) -> None:
        runner = PreflightRunner([_AlwaysFatalFail(), _AlwaysOk(), _AlwaysWarn()])
        report = runner.run_all(PreflightContext(profile=make_profile(tmp_path)))
        assert len(report.results) == 3


# OwnerSettingsInertCheck — 이식 대상. 원본 warn_inert_owner_settings() 특성화


def write_channels(profile: Profile, data: dict) -> None:
    path = profile.paths.channels
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class TestOwnerSettingsInertCheck:
    def test_설정이_없으면_통과한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        check = OwnerSettingsInertCheck()
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_소유자용_모델과_다른_채널_모델은_경고를_낸다(self, tmp_path: Path) -> None:
        profile = make_profile(
            tmp_path,
            primary_engine={
                "type": "claude",
                "binary": str(tmp_path / "claude_bin"),
                "model": "sonnet",
                "model_owner": "opus",
            },
        )
        write_channels(profile, {"C1": {"name": "잡담방", "model": "haiku"}})
        check = OwnerSettingsInertCheck()
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is False
        assert result.fatal is False
        assert "잡담방" in result.detail
        assert "haiku" in result.detail

    def test_effort가_소유자_하한보다_낮으면_경고를_낸다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        write_channels(profile, {"C1": {"name": "잡담방", "effort": "low"}})
        check = OwnerSettingsInertCheck()
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "잡담방" in result.detail
        assert "low" in result.detail

    def test_소유자_하한_이상의_effort는_경고하지_않는다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        write_channels(profile, {"C1": {"name": "분석방", "effort": "high"}})
        check = OwnerSettingsInertCheck()
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_codex_엔진에서도_경고를_낸다(self, tmp_path: Path) -> None:
        """AccessPolicy 는 엔진 종류와 무관하게 소유자의 채널 model 과 낮은
        effort 를 무시한다. 엔진별 예외를 두면 그 봇만 경고가 사라진다."""
        profile = make_profile(
            tmp_path,
            primary_engine={"type": "codex", "binary": "python3", "model": "m"},
        )
        write_channels(profile, {"C1": {"name": "잡담방", "model": "haiku", "effort": "low"}})
        check = OwnerSettingsInertCheck()
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "haiku" in result.detail
        assert "low" in result.detail


# EngineHomeCredentialCheck


class TestEngineHomeCredentialCheck:
    """봇별 홈을 가르면 그 홈에는 로그인 정보가 없다. 지금은 손으로 복사해야
    하는데, 안 하면 엔진이 답을 못 내는 것으로만 드러난다 (sca-kos.6).
    """

    def _프로필(self, tmp_path: Path, engine: str, home: Path) -> Profile:
        return make_profile(
            tmp_path,
            primary_engine={
                "type": engine,
                "binary": str(tmp_path / "bin"),
                "model": "m",
                "home_dir": str(home),
            },
        )

    def test_홈을_안_가르면_점검_대상이_아니다(self, tmp_path: Path) -> None:
        결과 = EngineHomeCredentialCheck().run(PreflightContext(make_profile(tmp_path)))
        assert 결과.ok

    def test_codex_홈에_auth_json_이_없으면_막는다(self, tmp_path: Path) -> None:
        홈 = tmp_path / "codex-home"
        홈.mkdir()
        결과 = EngineHomeCredentialCheck().run(
            PreflightContext(self._프로필(tmp_path, "codex", 홈))
        )
        assert not 결과.ok
        assert 결과.fatal
        assert "auth.json" in 결과.detail

    def test_codex_홈에_auth_json_이_있으면_통과한다(self, tmp_path: Path) -> None:
        홈 = tmp_path / "codex-home"
        홈.mkdir()
        자격 = 홈 / "auth.json"
        자격.write_text("{}", encoding="utf-8")
        자격.chmod(0o600)
        결과 = EngineHomeCredentialCheck().run(
            PreflightContext(self._프로필(tmp_path, "codex", 홈))
        )
        assert 결과.ok

    def test_다른_사용자가_읽을_수_있으면_막는다(self, tmp_path: Path) -> None:
        홈 = tmp_path / "codex-home"
        홈.mkdir()
        자격 = 홈 / "auth.json"
        자격.write_text("{}", encoding="utf-8")
        자격.chmod(0o604)
        결과 = EngineHomeCredentialCheck().run(
            PreflightContext(self._프로필(tmp_path, "codex", 홈))
        )
        assert not 결과.ok
        assert "권한" in 결과.detail

    def test_제미나이는_다른_파일을_본다(self, tmp_path: Path) -> None:
        홈 = tmp_path / "gemini-home"
        홈.mkdir()
        결과 = EngineHomeCredentialCheck().run(
            PreflightContext(self._프로필(tmp_path, "gemini", 홈))
        )
        assert not 결과.ok
        assert "antigravity-oauth-token" in 결과.detail

    def test_소유자가_못_읽는_파일도_막는다(self, tmp_path: Path) -> None:
        """000 과 100 은 group/other 비트가 없어 권한 검사를 빠져나간다."""
        홈 = tmp_path / "codex-home"
        홈.mkdir()
        자격 = 홈 / "auth.json"
        자격.write_text("{}", encoding="utf-8")
        for 권한 in (0o000, 0o100):
            자격.chmod(권한)
            결과 = EngineHomeCredentialCheck().run(
                PreflightContext(self._프로필(tmp_path, "codex", 홈))
            )
            assert not 결과.ok, oct(권한)
            assert "읽을 수 없다" in 결과.detail
        자격.chmod(0o600)

    def test_클로드는_홈을_가르는_것_자체를_지원하지_않는다(self, tmp_path: Path) -> None:
        """그래서 자격 선언이 비어 있다. 키체인과 CLAUDE_CODE_OAUTH_TOKEN 을
        쓰므로 홈에 둘 파일이 없다 (코덱스 리뷰).
        """
        assert ClaudeEnvironmentPolicy.CREDENTIAL_FILES == ()
        with pytest.raises(ConfigError):
            ClaudeEnvironmentPolicy(profile_name="example", home_dir=tmp_path)

    def test_홈을_못_가르는_엔진에_홈을_주면_막는다(self, tmp_path: Path) -> None:
        """자격 선언이 비었다고 통과시키면 preflight 는 기동 가능이라 하고
        첫 요청에서 실패한다 (코덱스 리뷰).
        """
        홈 = tmp_path / "claude-home"
        홈.mkdir()
        결과 = EngineHomeCredentialCheck().run(
            PreflightContext(self._프로필(tmp_path, "claude", 홈))
        )
        assert not 결과.ok
        assert "봇별 홈" in 결과.detail

    def test_안내에_복사할_원본_경로가_들어간다(self, tmp_path: Path) -> None:
        홈 = tmp_path / "codex-home"
        홈.mkdir()
        결과 = EngineHomeCredentialCheck().run(
            PreflightContext(self._프로필(tmp_path, "codex", 홈))
        )
        assert "~/.codex/auth.json" in 결과.detail

    def test_2차_엔진의_홈도_본다(self, tmp_path: Path) -> None:
        홈 = tmp_path / "codex-home"
        홈.mkdir()
        프로필 = make_profile(
            tmp_path,
            fallback_engine={
                "type": "codex",
                "binary": str(tmp_path / "bin"),
                "model": "m",
                "home_dir": str(홈),
            },
        )
        결과 = EngineHomeCredentialCheck().run(PreflightContext(프로필))
        assert not 결과.ok
        assert "2차" in 결과.detail

    def test_모르는_엔진은_점검을_건너뛴다(self, tmp_path: Path) -> None:
        결과 = EngineHomeCredentialCheck().run(
            PreflightContext(self._프로필(tmp_path, "없는엔진", tmp_path / "홈"))
        )
        assert 결과.ok


# suite.py — 표준 점검 목록을 한 자리에


class TestPreflightSuite:
    """점검 목록이 명령 안에만 있으면 기동 게이트가 따로 만들게 되고, 그
    순간 두 목록이 갈라진다(sca-s5r).
    """

    def test_표준_점검을_전부_담는다(self) -> None:
        from slack_cli_agent.preflight.suite import PreflightSuite

        이름들 = [c.name for c in PreflightSuite().checks]
        assert 이름들 == [
            "workdir",
            "engine_binary",
            "engine_home_credentials",
            "mcp_server",
            "prompt_files",
            "channel_settings_readable",
            "owner_settings_inert",
            "profile_permissions",
            "tool_allowlist_enforcement",
            "usage_check_command",
            "profile_unknown_keys",
            "channel_user_tools",
            "mcp_credentials",
        ]

    def test_추가_인자가_실제_점검에_반영된다(self, tmp_path: Path) -> None:
        """내부 속성이 아니라 점검 결과로 본다. 넘기기만 하고 쓰이지 않으면
        속성 단언은 통과하면서 점검은 아무것도 안 본다.
        """
        from slack_cli_agent.preflight.suite import PreflightSuite

        profile = make_profile(tmp_path)
        기본 = {r.detail for r in PreflightSuite().run(profile).results}
        추가 = {
            r.detail
            for r in PreflightSuite(
                required_prompts=["없는프롬프트"],
                extra_workdirs=["없는자리"],
            )
            .run(profile)
            .results
        }
        assert any("없는프롬프트" in d for d in 추가)
        assert any("없는자리" in d for d in 추가)
        assert not any("없는프롬프트" in d for d in 기본)

    def test_보고서를_낸다(self, tmp_path: Path) -> None:
        from slack_cli_agent.preflight.suite import PreflightSuite

        report = PreflightSuite().run(make_profile(tmp_path))
        assert len(report.results) == len(PreflightSuite().checks)

    def test_결과를_점검_이름과_함께_출력한다(self, tmp_path: Path) -> None:
        """출력 규칙이 명령에만 있으면 게이트의 출력이 달라진다."""
        import io

        from slack_cli_agent.preflight.suite import PreflightSuite

        suite = PreflightSuite()
        out = io.StringIO()
        report = suite.run(make_profile(tmp_path))
        suite.report_to(report, out)
        본문 = out.getvalue()
        for check in suite.checks:
            assert check.name in 본문
        assert "기동 불가" in 본문

    def test_통과하면_기동_가능을_출력한다(self, tmp_path: Path) -> None:
        import io

        from slack_cli_agent.preflight.runner import PreflightReport
        from slack_cli_agent.preflight.suite import PreflightSuite

        suite = PreflightSuite()
        out = io.StringIO()
        report = PreflightReport(
            results=tuple(CheckResult(ok=True, detail="통과") for _ in suite.checks)
        )
        suite.report_to(report, out)
        assert "기동 가능" in out.getvalue()

    def test_출력_전체가_정해진_형식이다(self) -> None:
        """이름 포함 여부만 보면 표식, 공백, 줄 순서가 바뀌어도 통과한다
        (코덱스 리뷰). 게이트가 같은 문구를 로그에 내야 하므로 형식 자체를
        고정한다.
        """
        import io

        from slack_cli_agent.preflight.runner import PreflightReport
        from slack_cli_agent.preflight.suite import PreflightSuite

        suite = PreflightSuite()
        results = [CheckResult(ok=True, detail="좋음") for _ in suite.checks]
        results[1] = CheckResult(ok=False, detail="나쁨")
        results[2] = CheckResult(ok=False, detail="주의", fatal=False)
        out = io.StringIO()
        suite.report_to(PreflightReport(results=tuple(results)), out)
        assert out.getvalue() == (
            "[통과] workdir : 좋음\n"
            "[실패] engine_binary : 나쁨\n"
            "[경고] engine_home_credentials : 주의\n"
            "[통과] mcp_server : 좋음\n"
            "[통과] prompt_files : 좋음\n"
            "[통과] channel_settings_readable : 좋음\n"
            "[통과] owner_settings_inert : 좋음\n"
            "[통과] profile_permissions : 좋음\n"
            "[통과] tool_allowlist_enforcement : 좋음\n"
            "[통과] usage_check_command : 좋음\n"
            "[통과] profile_unknown_keys : 좋음\n"
            "[통과] channel_user_tools : 좋음\n"
            "[통과] mcp_credentials : 좋음\n"
            "기동 불가\n"
        )

    def test_messages가_판정줄에는_결과를_안_붙인다(self) -> None:
        """게이트가 로그 레벨을 고르려면 어느 줄이 어느 결과인지 알아야 한다."""
        from slack_cli_agent.preflight.runner import PreflightReport
        from slack_cli_agent.preflight.suite import PreflightSuite

        suite = PreflightSuite()
        results = tuple(CheckResult(ok=True, detail="좋음") for _ in suite.checks)
        쌍 = list(suite.messages(PreflightReport(results=results)))
        assert [r for r, _ in 쌍[:-1]] == list(results)
        assert 쌍[-1][0] is None
        assert 쌍[-1][1] == "기동 가능"

    def test_경고는_실패와_다르게_표시한다(self, tmp_path: Path) -> None:
        import io

        from slack_cli_agent.preflight.runner import PreflightReport
        from slack_cli_agent.preflight.suite import PreflightSuite

        suite = PreflightSuite()
        out = io.StringIO()
        results = [CheckResult(ok=True, detail="통과") for _ in suite.checks]
        results[0] = CheckResult(ok=False, detail="주의", fatal=False)
        suite.report_to(PreflightReport(results=tuple(results)), out)
        본문 = out.getvalue()
        assert "[경고]" in 본문
        assert "[실패]" not in 본문
        assert "기동 가능" in 본문


class _터지는점검(PreflightCheck):
    name = "터지는점검"

    def run(self, ctx: PreflightContext) -> CheckResult:
        raise OSError("디스크가 응답하지 않음")


class TestRunner예외처리:
    """점검 하나가 예상 밖 예외를 던지면 run_all 이 그대로 터져 나머지
    점검 결과까지 사라졌다. '첫 실패에서 멈추지 않는다' 는 이 클래스의
    계약과 어긋난다(sca-ylz).
    """

    def test_예외를_fatal_결과로_바꾼다(self, tmp_path: Path) -> None:
        runner = PreflightRunner([_터지는점검()])
        report = runner.run_all(PreflightContext(profile=make_profile(tmp_path)))
        assert report.bootable is False
        assert "디스크가 응답하지 않음" in report.results[0].detail

    def test_사유가_빈_예외여도_종류가_남는다(self, tmp_path: Path) -> None:
        """str(exc) 가 빈 내장 예외가 여럿이다. 그대로 쓰면 운영자가 보는
        사유가 빈칸이 된다 (코덱스 리뷰).
        """

        class _조용히터지는점검(PreflightCheck):
            name = "조용히터지는점검"

            def run(self, ctx: PreflightContext) -> CheckResult:
                raise RecursionError

        report = PreflightRunner([_조용히터지는점검()]).run_all(
            PreflightContext(profile=make_profile(tmp_path))
        )
        assert "RecursionError" in report.results[0].detail

    def test_터진_뒤에도_나머지_점검을_돈다(self, tmp_path: Path) -> None:
        runner = PreflightRunner([_터지는점검(), _AlwaysOk()])
        report = runner.run_all(PreflightContext(profile=make_profile(tmp_path)))
        assert len(report.results) == 2
        assert report.results[1].ok is True


class Test검사_목록_주입:
    """대역이 _checks 를 덮어쓰지 않게 공개 주입 지점을 연다(sca-mb7).

    검사 목록만 갈아 끼우고 판정과 출력은 진짜 것을 그대로 쓰는 것이 목적이다.
    private 이름을 덮으면 Suite 내부 저장 방식이 바뀔 때 대역이 조용히 깨진다.
    """

    def _프로필(self, tmp_path: Path) -> Profile:
        return Profile.from_dict(
            {
                "name": "example",
                "primary_engine": {"type": "claude", "binary": "claude", "model": "m"},
                "owner_user_id": "U1",
                "troubleshoot_channel": "C1",
                "state_dir": str(tmp_path / "state"),
            }
        )

    def test_주입한_검사만_돈다(self, tmp_path: Path) -> None:
        from preflight_support import 고정점검

        from slack_cli_agent.preflight.suite import PreflightSuite

        suite = PreflightSuite(checks=(고정점검(ok=True),))
        assert [c.name for c in suite.checks] == ["고정"]
        assert suite.run(self._프로필(tmp_path)).bootable is True

    def test_주입해도_판정과_출력은_진짜_것을_쓴다(self, tmp_path: Path) -> None:
        import io

        from preflight_support import 고정점검

        from slack_cli_agent.preflight.suite import PreflightSuite

        suite = PreflightSuite(checks=(고정점검(ok=False),))
        보고 = suite.run(self._프로필(tmp_path))
        출력 = io.StringIO()
        suite.report_to(보고, 출력)
        assert "[실패] 고정" in 출력.getvalue()
        assert "기동 불가" in 출력.getvalue()

    def test_안_주입하면_표준_검사를_쓴다(self, tmp_path: Path) -> None:
        from slack_cli_agent.preflight.suite import PreflightSuite

        assert len(PreflightSuite().checks) > 1

    def test_대역이_private_이름을_안_건드린다(self) -> None:
        """이 시험이 곧 재발 방지다 — 덮어쓰기로 되돌아가면 여기서 걸린다."""
        import inspect

        import preflight_support

        assert "_checks" not in inspect.getsource(preflight_support)


# ToolAllowlistEnforcementCheck — 허용목록을 강제하지 못하는 엔진을 기동 때 알린다


class TestToolAllowlistEnforcementCheck:
    """도구 허용목록은 엔진마다 실효가 다르다(sca-dyb.11).

    호출자는 모든 엔진에 같은 목록을 만들어 넘기지만 강제하는 것은 클로드뿐이다.
    코덱스는 샌드박스라는 다른 축이고 제미나이는 아무것도 강제하지 않는다. 요청마다
    감사에 남기면 그 봇에서는 매 요청이 같은 사실을 반복하므로, 설정 시점에 한 번
    알린다.
    """

    def test_클로드는_허용목록을_강제하므로_통과한다(self, tmp_path: Path) -> None:
        result = ToolAllowlistEnforcementCheck().run(
            PreflightContext(profile=make_profile(tmp_path))
        )
        assert result.ok is True

    def test_제미나이는_아무것도_강제하지_않는다고_알린다(self, tmp_path: Path) -> None:
        profile = make_profile(
            tmp_path,
            primary_engine={"type": "gemini", "binary": str(tmp_path / "agy"), "model": "m"},
        )
        result = ToolAllowlistEnforcementCheck().run(PreflightContext(profile=profile))
        assert result.ok is False
        assert result.fatal is False
        assert "gemini" in result.detail
        assert "none" in result.detail

    def test_코덱스는_샌드박스라는_다른_축임을_알린다(self, tmp_path: Path) -> None:
        profile = make_profile(
            tmp_path,
            primary_engine={"type": "codex", "binary": str(tmp_path / "codex"), "model": "m"},
        )
        result = ToolAllowlistEnforcementCheck().run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "coarse_sandbox" in result.detail

    def test_2차_엔진도_함께_본다(self, tmp_path: Path) -> None:
        """한도 소진 뒤에는 2차 엔진이 같은 요청을 받는다. 그때 허용목록이
        풀리는 것을 켤 때 알아야 한다."""
        profile = make_profile(
            tmp_path,
            fallback_engine={"type": "codex", "binary": str(tmp_path / "codex"), "model": "m"},
        )
        result = ToolAllowlistEnforcementCheck().run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "2차" in result.detail

    def test_모르는_엔진이면_판정하지_않는다(self, tmp_path: Path) -> None:
        """플러그인이 더한 엔진은 이 목록에 없다. 없는 것을 강제 없음으로 읽으면
        거짓 경고가 된다."""
        profile = make_profile(
            tmp_path,
            primary_engine={"type": "플러그인엔진", "binary": str(tmp_path / "x"), "model": "m"},
        )
        result = ToolAllowlistEnforcementCheck().run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_표준_점검_목록에_들어_있다(self) -> None:
        names = [check.name for check in PreflightSuite().checks]
        assert ToolAllowlistEnforcementCheck.name in names


class TestUsageCheckCommandCheck:
    """명령이 잘못 적혀 있으면 매 시간 실패 로그만 남고 아무도 안 본다
    (sca-3p7). 기동 때 한 번 드러낸다.
    """

    def test_명령이_없으면_점검_대상이_아니다(self, tmp_path: Path) -> None:
        from slack_cli_agent.preflight.checks import UsageCheckCommandCheck

        result = UsageCheckCommandCheck().run(PreflightContext(profile=make_profile(tmp_path)))

        assert result.ok is True

    def test_절대경로_실행파일이_없으면_실패한다(self, tmp_path: Path) -> None:
        from slack_cli_agent.preflight.checks import UsageCheckCommandCheck

        profile = make_profile(
            tmp_path, usage_check_command=[str(tmp_path / "no_such"), "--check"]
        )

        result = UsageCheckCommandCheck().run(PreflightContext(profile=profile))

        assert result.ok is False
        assert "no_such" in result.detail
        # 사용량 확인이 안 돼도 봇은 답할 수 있다. 기동을 막지 않는다.
        assert result.fatal is False

    def test_실행_권한이_없으면_실패한다(self, tmp_path: Path) -> None:
        from slack_cli_agent.preflight.checks import UsageCheckCommandCheck

        binary = tmp_path / "usage.py"
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(0o600)
        profile = make_profile(tmp_path, usage_check_command=[str(binary)])

        result = UsageCheckCommandCheck().run(PreflightContext(profile=profile))

        assert result.ok is False
        assert "실행 권한" in result.detail

    def test_실행파일이_있으면_통과한다(self, tmp_path: Path) -> None:
        from slack_cli_agent.preflight.checks import UsageCheckCommandCheck

        binary = tmp_path / "usage.py"
        make_executable(binary)
        profile = make_profile(tmp_path, usage_check_command=[str(binary), "--check"])

        result = UsageCheckCommandCheck().run(PreflightContext(profile=profile))

        assert result.ok is True

    def test_PATH로_찾는_이름이면_which로_확인한다(self, tmp_path: Path) -> None:
        from slack_cli_agent.preflight.checks import UsageCheckCommandCheck

        profile = make_profile(tmp_path, usage_check_command=["python3", "-V"])

        result = UsageCheckCommandCheck().run(PreflightContext(profile=profile))

        assert result.ok is True


class TestChannelUserToolsCheck:
    """사용자별 추가 허용 도구 표의 키를 잘못 적으면 그 사람에게 도구가 안
    붙는데 로그도 오류도 없다 (sca-34o). 도구 이름 자체는 엔진마다 달라 여기서
    보지 않는다.
    """

    def _check(self):  # type: ignore[no-untyped-def]
        from slack_cli_agent.preflight.checks import ChannelUserToolsCheck

        return ChannelUserToolsCheck()

    def test_표가_없으면_통과한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        write_channels(profile, {"C1": {"name": "잡담방"}})

        assert self._check().run(PreflightContext(profile=profile)).ok is True

    def test_사용자_ID_형식이면_통과한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        write_channels(
            profile, {"C1": {"name": "잡담방", "user_tools": {"U012ABC": ["Bash(a.sh:*)"]}}}
        )

        assert self._check().run(PreflightContext(profile=profile)).ok is True

    def test_사용자_ID_형식이_아니면_경고한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        write_channels(
            profile, {"C1": {"name": "잡담방", "user_tools": {"홍길동": ["Bash(a.sh:*)"]}}}
        )

        result = self._check().run(PreflightContext(profile=profile))

        assert result.ok is False
        assert result.fatal is False
        assert "홍길동" in result.detail
        assert "잡담방" in result.detail

    def test_도구_목록이_비면_경고한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        write_channels(profile, {"C1": {"name": "잡담방", "user_tools": {"U012ABC": []}}})

        result = self._check().run(PreflightContext(profile=profile))

        assert result.ok is False
        assert "U012ABC" in result.detail


class TestProfileUnknownKeysCheck:
    """한 글자 틀린 키는 기동을 막지 않고 그 기능만 끈다. 미설정과 오타가 같은
    모습이 되는 것을 기동 시점에 드러낸다 (sca-4dr)."""

    def write(self, tmp_path: Path, data: dict) -> Profile:
        base = {
            "name": "example",
            "primary_engine": {
                "type": "claude",
                "binary": str(tmp_path / "claude_bin"),
                "model": "m",
            },
            "owner_user_id": "U1",
            "troubleshoot_channel": "C1",
            "state_dir": str(tmp_path / "state"),
        }
        base.update(data)
        (tmp_path / "example.json").write_text(json.dumps(base), encoding="utf-8")
        return Profile.load("example", [tmp_path])

    def test_모르는_최상위_키를_알린다(self, tmp_path: Path) -> None:
        profile = self.write(tmp_path, {"owner_dm_id": "D1"})
        result = ProfileUnknownKeysCheck().run(PreflightContext(profile=profile))
        assert result.ok is False
        assert result.fatal is False
        assert "owner_dm_id" in result.detail

    def test_settings_블록의_모르는_키도_알린다(self, tmp_path: Path) -> None:
        """sca-39c 가 이 모양이었다. owner_only_channels 를 단수로 쓰면 느린 요청
        보고가 조용히 꺼진다."""
        profile = self.write(tmp_path, {"settings": {"owner_only_channel": ["C1"]}})
        result = ProfileUnknownKeysCheck().run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "owner_only_channel" in result.detail
        assert "settings" in result.detail

    def test_아는_키만_있으면_통과한다(self, tmp_path: Path) -> None:
        profile = self.write(
            tmp_path, {"owner_dm": "D1", "settings": {"owner_only_channels": ["C1"]}}
        )
        result = ProfileUnknownKeysCheck().run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_파일에서_온_프로필이_아니면_판정하지_않는다(self, tmp_path: Path) -> None:
        """웹 콘솔이 메모리에서 만든 프로필에는 읽을 원본이 없다. 없는 것을
        키 없음으로 읽으면 거짓 경고가 된다."""
        result = ProfileUnknownKeysCheck().run(PreflightContext(profile=make_profile(tmp_path)))
        assert result.ok is True

    def test_원본을_못_읽으면_판정하지_않는다(self, tmp_path: Path) -> None:
        profile = self.write(tmp_path, {})
        (tmp_path / "example.json").unlink()
        result = ProfileUnknownKeysCheck().run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_표준_점검_목록에_들어_있다(self) -> None:
        names = [check.name for check in PreflightSuite().checks]
        assert ProfileUnknownKeysCheck.name in names


class TestProfileKnownKeys:
    def test_from_dict_가_읽는_키가_전부_들어_있다(self) -> None:
        """KNOWN_KEYS 가 from_dict 와 어긋나면 이 점검이 정상 설정을 오타로
        보고한다. 키를 더할 때 두 곳을 같이 고치게 한다."""
        import inspect
        import re as _re

        source = inspect.getsource(Profile.from_dict) + inspect.getsource(Profile._under)
        read = set(_re.findall(r'data\.get\(\s*"([a-z_]+)"', source))
        read |= set(_re.findall(r'_under\(data,\s*"([a-z_]+)"', source))
        assert read, "from_dict 에서 읽는 키를 하나도 못 찾았다"
        assert read <= PROFILE_KNOWN_KEYS


class TestChannelSettingsReadableCheck:
    """조회 경로는 잘못된 값을 안전한 쪽으로 읽고 넘어간다(sca-xe0). 그러면
    로그만 남고 아무도 안 본다. 부트에서 한 번 경고를 내는 자리가 필요하다."""

    def test_설정이_없으면_통과한다(self, tmp_path: Path) -> None:
        result = ChannelSettingsReadableCheck().run(PreflightContext(profile=make_profile(tmp_path)))
        assert result.ok is True

    def test_읽을_수_있는_값은_통과한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        write_channels(profile, {"C1": {"name": "잡담방", "tool_enforcement": "strict"}})
        result = ChannelSettingsReadableCheck().run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_잘못된_값은_채널과_함께_경고한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        write_channels(profile, {"C1": {"name": "잡담방", "tool_enforcement": "strcit"}})
        result = ChannelSettingsReadableCheck().run(PreflightContext(profile=profile))
        assert result.ok is False
        assert result.fatal is False
        assert "C1" in result.detail
        assert "strcit" in result.detail

    def test_경고여도_부팅을_막지_않는다(self, tmp_path: Path) -> None:
        """그 채널은 이미 strict 로 떨어져 돈다. 무관한 채널까지 봇을 내리는
        것이 이 이슈가 고치려는 것이다."""
        profile = make_profile(tmp_path)
        write_channels(profile, {"C1": {"tool_enforcement": 3}, "C2": {"mode": "helpdesk"}})
        result = ChannelSettingsReadableCheck().run(PreflightContext(profile=profile))
        assert result.fatal is False
