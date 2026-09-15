"""기동 전 점검. PreflightCheck, PreflightRunner, 구체 점검 4종.

`McpServerCheck._mcp_ready` 는 원본 `bot.py` 의 `mcp_ready()` 를 그대로
옮긴 이식 대상이다. 입력·기대 출력은 원본 소스를 읽어 로직을 따라간
것이다 — 실행 파일이 없으면 "실행 파일 없음", 셰뱅 인터프리터를 PATH 에서
못 찾으면 그 이름으로 실패 문구를 낸다.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from slack_cli_agent.config.profile import Profile
from slack_cli_agent.core.errors import ConfigError
from slack_cli_agent.engine.environment import ClaudeEnvironmentPolicy
from slack_cli_agent.preflight.check import CheckResult, PreflightCheck, PreflightContext
from slack_cli_agent.preflight.checks import (
    EngineBinaryCheck,
    EngineHomeCredentialCheck,
    McpServerCheck,
    OwnerSettingsInertCheck,
    PromptFileCheck,
    WorkdirCheck,
)
from slack_cli_agent.preflight.runner import PreflightRunner


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


# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# WorkdirCheck


class TestWorkdirCheck:
    def test_작업_자리가_없으면_실패한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path, work_root=str(tmp_path / "work" / "missing"))
        check = WorkdirCheck()
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "없다" in result.detail

    def test_작업_자리가_홈_안이면_실패한다(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


# ---------------------------------------------------------------------------
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
            fallback_engine={"type": "codex", "binary": str(tmp_path / "no_such_binary"), "model": "m2"},
        )
        check = EngineBinaryCheck()
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "2차" in result.detail


# ---------------------------------------------------------------------------
# McpServerCheck — 이식 대상. mcp_ready() 본문 특성화


class TestMcpServerCheck:
    def test_설정_파일이_없으면_경고만이고_기동을_막지_않는다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        check = McpServerCheck()
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is True
        assert result.fatal is False

    def test_실행_파일이_없는_서버는_실패한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        mcp_config = profile.paths.mcp_config
        mcp_config.parent.mkdir(parents=True)
        mcp_config.write_text(
            json.dumps({"mcpServers": {"broken": {"command": str(tmp_path / "no_such")}}}),
            encoding="utf-8",
        )
        check = McpServerCheck()
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "broken" in result.detail
        assert "실행 파일 없음" in result.detail

    def test_셰뱅_인터프리터를_PATH에서_못_찾으면_실패한다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script = tmp_path / "server.js"
        script.write_text("#!/usr/bin/env definitely-not-on-path\nconsole.log(1)\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        profile = make_profile(tmp_path)
        mcp_config = profile.paths.mcp_config
        mcp_config.parent.mkdir(parents=True)
        mcp_config.write_text(
            json.dumps({"mcpServers": {"js-server": {"command": str(script)}}}),
            encoding="utf-8",
        )
        check = McpServerCheck()
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "definitely-not-on-path" in result.detail

    def test_설정을_읽지_못하면_실패한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        mcp_config = profile.paths.mcp_config
        mcp_config.parent.mkdir(parents=True)
        mcp_config.write_text("이건 json이 아니다", encoding="utf-8")
        check = McpServerCheck()
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "읽지 못했습니다" in result.detail

    def test_command이_없는_서버는_건너뛴다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        mcp_config = profile.paths.mcp_config
        mcp_config.parent.mkdir(parents=True)
        mcp_config.write_text(
            json.dumps({"mcpServers": {"url-only": {"serverUrl": "https://example.test"}}}),
            encoding="utf-8",
        )
        check = McpServerCheck()
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is True


# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
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
                "type": "claude", "binary": str(tmp_path / "claude_bin"),
                "model": "sonnet", "model_owner": "opus",
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

    def test_codex_엔진이면_건너뛴다(self, tmp_path: Path) -> None:
        profile = make_profile(
            tmp_path,
            primary_engine={"type": "codex", "binary": "python3", "model": "m"},
        )
        write_channels(profile, {"C1": {"name": "잡담방", "model": "haiku", "effort": "low"}})
        check = OwnerSettingsInertCheck()
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is True


# ---------------------------------------------------------------------------
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
