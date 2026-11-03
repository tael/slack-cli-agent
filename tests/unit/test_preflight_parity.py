"""B6 — 기동 점검 확충. 마메치 check.py 의 관측 동작(실패 문구 포함)에 맞춰
이식한 9종 점검.

각 점검은 개별 호출·시험 가능해야 하고, 대상 자원이 없으면 경고/생략으로
내려가야 한다(크래시 금지). 조직 특화 값(어떤 MCP 서버가 같은 시스템을
가리키는지, 어떤 프롬프트 낱말이 어떤 도구를 약속하는지, capabilities.json
등급 체계)은 공통 골격에 넣지 않고 생성자 주입점으로 남겨 조직이 채운다.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from slack_cli_agent.config.profile import Profile
from slack_cli_agent.preflight.check import PreflightContext
from slack_cli_agent.preflight.checks import (
    CodexConfigLoadCheck,
    DuplicateMcpSystemCheck,
    FullDiskAccessCheck,
    McpScriptOwnershipCheck,
    PluginAstCheck,
    PromptToolConsistencyCheck,
    ScriptCapabilityGradeCheck,
    SkillSymlinkCheck,
    SlackApiRegistrationCheck,
)
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


def make_executable(path: Path, body: str = "#!/bin/sh\necho ok\n") -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


# PluginAstCheck — G29. bot.py AST 미정의이름 검사의 이식. 이 저장소에는 단일
# bot.py 가 없고, 기동 때 임포트되는 조직 코드는 플러그인 모듈이다.


class TestPluginAstCheck:
    def _install_module(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, body: str) -> None:
        pkg_dir = tmp_path / "plugin_mods"
        pkg_dir.mkdir(exist_ok=True)
        (pkg_dir / f"{name}.py").write_text(body, encoding="utf-8")
        monkeypatch.syspath_prepend(str(pkg_dir))
        import importlib

        importlib.invalidate_caches()
        sys.modules.pop(name, None)

    def test_플러그인이_없으면_통과한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        result = PluginAstCheck().run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_미정의_이름이_있으면_실패한다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._install_module(
            tmp_path, monkeypatch, "broken_plugin_mod",
            "def handler():\n    return 어딘가에도_없는_이름\n",
        )
        profile = make_profile(tmp_path, plugins=["broken_plugin_mod"])
        result = PluginAstCheck().run(PreflightContext(profile=profile))
        assert result.ok is False
        assert result.fatal is True
        assert "어딘가에도_없는_이름" in result.detail

    def test_정의된_이름만_쓰면_통과한다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._install_module(
            tmp_path, monkeypatch, "clean_plugin_mod",
            "import os\n\n\ndef handler(x):\n    y = x + 1\n    return os.path.join(str(y), '.')\n",
        )
        profile = make_profile(tmp_path, plugins=["clean_plugin_mod"])
        result = PluginAstCheck().run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_모듈을_못_찾으면_건너뛴다(self, tmp_path: Path) -> None:
        """모듈 로드 실패는 PluginLoader 의 몫이다 — 여기서 겹쳐 보고하지 않는다."""
        profile = make_profile(tmp_path, plugins=["없는모듈_zzz_plugin"])
        result = PluginAstCheck().run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_콜론으로_클래스를_붙인_경로도_모듈만_본다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._install_module(
            tmp_path, monkeypatch, "colon_plugin_mod",
            "class Plugin:\n    pass\n",
        )
        profile = make_profile(tmp_path, plugins=["colon_plugin_mod:Plugin"])
        result = PluginAstCheck().run(PreflightContext(profile=profile))
        assert result.ok is True


# SkillSymlinkCheck — G31. ALWAYS_SKILLS 심링크 실재 점검의 이식.


class TestSkillSymlinkCheck:
    def test_스킬_자리가_없으면_통과한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        result = SkillSymlinkCheck().run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_심링크가_없으면_통과한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        profile.paths.skill_files.mkdir(parents=True)
        result = SkillSymlinkCheck().run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_정상_심링크는_통과한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        profile.paths.skill_files.mkdir(parents=True)
        real = tmp_path / "real-skill"
        real.mkdir()
        (real / "SKILL.md").write_text("내용", encoding="utf-8")
        (profile.paths.skill_files / "my-skill").symlink_to(real)
        result = SkillSymlinkCheck().run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_깨진_심링크는_실패한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        profile.paths.skill_files.mkdir(parents=True)
        missing_target = tmp_path / "gone-skill"
        (profile.paths.skill_files / "my-skill").symlink_to(missing_target)
        result = SkillSymlinkCheck().run(PreflightContext(profile=profile))
        assert result.ok is False
        assert result.fatal is True
        assert "my-skill" in result.detail

    def test_SKILL_MD가_없으면_실패한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        profile.paths.skill_files.mkdir(parents=True)
        real = tmp_path / "real-skill-empty"
        real.mkdir()
        (profile.paths.skill_files / "my-skill").symlink_to(real)
        result = SkillSymlinkCheck().run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "SKILL.md" in result.detail

    def test_일반_디렉터리는_보지_않는다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        profile.paths.skill_files.mkdir(parents=True)
        (profile.paths.skill_files / "copied-skill").mkdir()
        result = SkillSymlinkCheck().run(PreflightContext(profile=profile))
        assert result.ok is True


# CodexConfigLoadCheck — G32. 대체엔진(Codex) 설정 실로드 검증의 이식.


class TestCodexConfigLoadCheck:
    def test_codex_엔진이_없으면_통과한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        result = CodexConfigLoadCheck().run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_codex_홈을_안_가르면_통과한다(self, tmp_path: Path) -> None:
        profile = make_profile(
            tmp_path,
            primary_engine={"type": "codex", "binary": "codex", "model": "m"},
        )
        result = CodexConfigLoadCheck().run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_설정_파일이_없으면_실패한다(self, tmp_path: Path) -> None:
        home = tmp_path / "codex-home"
        home.mkdir()
        profile = make_profile(
            tmp_path,
            primary_engine={
                "type": "codex", "binary": "codex", "model": "m", "home_dir": str(home),
            },
        )
        result = CodexConfigLoadCheck().run(PreflightContext(profile=profile))
        assert result.ok is False
        assert result.fatal is True
        assert "config.toml" in result.detail

    def test_바이너리를_못_찾으면_실패한다(self, tmp_path: Path) -> None:
        home = tmp_path / "codex-home"
        home.mkdir()
        (home / "config.toml").write_text("", encoding="utf-8")
        profile = make_profile(
            tmp_path,
            primary_engine={
                "type": "codex", "binary": str(tmp_path / "no_such_codex"),
                "model": "m", "home_dir": str(home),
            },
        )
        result = CodexConfigLoadCheck().run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "실행 파일" in result.detail

    def test_실행이_성공하면_통과한다(self, tmp_path: Path) -> None:
        home = tmp_path / "codex-home"
        home.mkdir()
        (home / "config.toml").write_text("", encoding="utf-8")
        binary = tmp_path / "fake_codex_ok"
        make_executable(binary, "#!/bin/sh\necho '[]'\nexit 0\n")
        profile = make_profile(
            tmp_path,
            primary_engine={
                "type": "codex", "binary": str(binary), "model": "m", "home_dir": str(home),
            },
        )
        result = CodexConfigLoadCheck().run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_실행이_실패하면_꼬리를_담아_실패한다(self, tmp_path: Path) -> None:
        home = tmp_path / "codex-home"
        home.mkdir()
        (home / "config.toml").write_text("", encoding="utf-8")
        binary = tmp_path / "fake_codex_fail"
        make_executable(binary, "#!/bin/sh\necho '이상한 서버' 1>&2\nexit 1\n")
        profile = make_profile(
            tmp_path,
            primary_engine={
                "type": "codex", "binary": str(binary), "model": "m", "home_dir": str(home),
            },
        )
        result = CodexConfigLoadCheck().run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "이상한 서버" in result.detail

    def test_시간_초과면_실패한다(self, tmp_path: Path) -> None:
        home = tmp_path / "codex-home"
        home.mkdir()
        (home / "config.toml").write_text("", encoding="utf-8")
        binary = tmp_path / "fake_codex_slow"
        make_executable(binary, "#!/bin/sh\nsleep 5\n")
        profile = make_profile(
            tmp_path,
            primary_engine={
                "type": "codex", "binary": str(binary), "model": "m", "home_dir": str(home),
            },
        )
        result = CodexConfigLoadCheck(timeout=0.2).run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "시간" in result.detail

    def test_2차_엔진도_본다(self, tmp_path: Path) -> None:
        home = tmp_path / "codex-home"
        home.mkdir()
        profile = make_profile(
            tmp_path,
            fallback_engine={
                "type": "codex", "binary": "codex", "model": "m", "home_dir": str(home),
            },
        )
        result = CodexConfigLoadCheck().run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "config.toml" in result.detail


# McpScriptOwnershipCheck — G33. mcp 스크립트 타봇 상태경로 참조 점검의 이식.


class TestMcpScriptOwnershipCheck:
    def test_로컬_스크립트가_없으면_통과한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        result = McpScriptOwnershipCheck().run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_원본을_모르면_비교할_것이_없다(self, tmp_path: Path) -> None:
        """from_dict 로 만든 프로필에는 source_file 이 없다."""
        script = tmp_path / "server.py"
        script.write_text("print('hi')", encoding="utf-8")
        profile = make_profile(tmp_path, mcp_servers={"서버": {"command": str(script)}})
        result = McpScriptOwnershipCheck().run(PreflightContext(profile=profile))
        assert result.ok is True

    def _write_profiles(self, tmp_path: Path, script_text: str) -> Profile:
        script = tmp_path / "server.py"
        script.write_text(script_text, encoding="utf-8")
        other = {
            "name": "other-bot",
            "primary_engine": {"type": "claude", "binary": "claude", "model": "m"},
            "owner_user_id": "U2",
            "troubleshoot_channel": "C2",
            "state_dir": str(tmp_path / ".other-bot"),
        }
        (tmp_path / "other-bot.json").write_text(json.dumps(other), encoding="utf-8")
        mine = {
            "name": "example",
            "primary_engine": {"type": "claude", "binary": "claude", "model": "m"},
            "owner_user_id": "U1",
            "troubleshoot_channel": "C1",
            "state_dir": str(tmp_path / ".example"),
            "mcp_servers": {"서버": {"command": str(script)}},
        }
        (tmp_path / "example.json").write_text(json.dumps(mine), encoding="utf-8")
        return Profile.load("example", [tmp_path])

    def test_다른_봇의_상태경로를_참조하면_실패한다(self, tmp_path: Path) -> None:
        profile = self._write_profiles(
            tmp_path, "STATE = '/wherever/.other-bot/token.json'\n"
        )
        result = McpScriptOwnershipCheck().run(PreflightContext(profile=profile))
        assert result.ok is False
        assert result.fatal is True
        assert "other-bot" in result.detail

    def test_자기_자신의_경로만_참조하면_통과한다(self, tmp_path: Path) -> None:
        profile = self._write_profiles(tmp_path, "STATE = '/wherever/.example/token.json'\n")
        result = McpScriptOwnershipCheck().run(PreflightContext(profile=profile))
        assert result.ok is True


# PromptToolConsistencyCheck — G35. 프롬프트 약속-도구 일치 점검. 낱말→도구
# 약속 표는 조직 지식이라 생성자 주입으로 받는다.


class TestPromptToolConsistencyCheck:
    def test_약속표가_없으면_통과한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        result = PromptToolConsistencyCheck().run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_프롬프트_자리가_없으면_통과한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        check = PromptToolConsistencyCheck(
            promises={"웹 검색": ("WebSearch",)}, base_tools=("Read",)
        )
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_약속한_도구가_공통목록에_없으면_실패한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        profile.paths.prompts.mkdir(parents=True)
        (profile.paths.prompts / "persona.md").write_text(
            "이 봇은 웹 검색을 수행한다.", encoding="utf-8"
        )
        check = PromptToolConsistencyCheck(
            promises={"웹 검색": ("WebSearch",)}, base_tools=("Read",)
        )
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is False
        assert result.fatal is True
        assert "WebSearch" in result.detail
        assert "persona.md" in result.detail

    def test_약속한_도구가_공통목록에_있으면_통과한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        profile.paths.prompts.mkdir(parents=True)
        (profile.paths.prompts / "persona.md").write_text(
            "이 봇은 웹 검색을 수행한다.", encoding="utf-8"
        )
        check = PromptToolConsistencyCheck(
            promises={"웹 검색": ("WebSearch",)}, base_tools=("Read", "WebSearch")
        )
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_낱말이_없으면_통과한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        profile.paths.prompts.mkdir(parents=True)
        (profile.paths.prompts / "persona.md").write_text("평범한 안내문", encoding="utf-8")
        check = PromptToolConsistencyCheck(
            promises={"웹 검색": ("WebSearch",)}, base_tools=()
        )
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is True


# DuplicateMcpSystemCheck — G36. 같은 시스템 중복 MCP 서버 점검. 어떤 서버
# 이름들이 같은 시스템을 가리키는지는 조직 지식이라 생성자 주입으로 받는다.


class TestDuplicateMcpSystemCheck:
    def test_그룹_설정이_없으면_통과한다(self, tmp_path: Path) -> None:
        profile = make_profile(
            tmp_path, mcp_servers={"jira": {"command": "a"}, "atlassian": {"command": "b"}}
        )
        result = DuplicateMcpSystemCheck().run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_같은_시스템을_가리키는_서버가_둘이면_실패한다(self, tmp_path: Path) -> None:
        profile = make_profile(
            tmp_path, mcp_servers={"jira": {"command": "a"}, "atlassian": {"command": "b"}}
        )
        check = DuplicateMcpSystemCheck(groups={"jira": ("jira", "atlassian")})
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is False
        assert result.fatal is True
        assert "jira" in result.detail
        assert "atlassian" in result.detail

    def test_하나만_있으면_통과한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path, mcp_servers={"jira": {"command": "a"}})
        check = DuplicateMcpSystemCheck(groups={"jira": ("jira", "atlassian")})
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_꺼둔_서버는_세지_않는다(self, tmp_path: Path) -> None:
        profile = make_profile(
            tmp_path,
            mcp_servers={
                "jira": {"command": "a"},
                "atlassian": {"command": "b", "disabled": True},
            },
        )
        check = DuplicateMcpSystemCheck(groups={"jira": ("jira", "atlassian")})
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is True


# FullDiskAccessCheck — G38. venv 바이너리 FDA 여부/실경로 추적의 이식.


class TestFullDiskAccessCheck:
    def test_macOS가_아니면_해당없음으로_통과한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        check = FullDiskAccessCheck(platform_name="linux", binary=tmp_path / "python")
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is True
        assert result.fatal is False

    def test_TCC_DB를_못_읽으면_권한없음을_경고한다(self, tmp_path: Path) -> None:
        tcc = tmp_path / "TCC.db"
        tcc.write_text("x", encoding="utf-8")
        tcc.chmod(0o000)
        try:
            profile = make_profile(tmp_path)
            check = FullDiskAccessCheck(
                platform_name="darwin", binary=tmp_path / "python", tcc_db=tcc
            )
            result = check.run(PreflightContext(profile=profile))
        finally:
            tcc.chmod(0o600)
        assert result.ok is False
        assert result.fatal is False
        assert "전체 디스크 접근" in result.detail

    def test_TCC_DB를_읽을_수_있으면_통과한다(self, tmp_path: Path) -> None:
        tcc = tmp_path / "TCC.db"
        tcc.write_bytes(b"0123456789abcdef")
        profile = make_profile(tmp_path)
        check = FullDiskAccessCheck(platform_name="darwin", binary=tmp_path / "python", tcc_db=tcc)
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is True
        assert "있음" in result.detail

    def test_TCC_DB_자체가_없으면_판정불가로_통과한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        check = FullDiskAccessCheck(
            platform_name="darwin", binary=tmp_path / "python", tcc_db=tmp_path / "no-such-tcc"
        )
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is True
        assert "판정 불가" in result.detail

    def test_바이너리_경로가_바뀌면_알린다(self, tmp_path: Path) -> None:
        tcc = tmp_path / "TCC.db"
        tcc.write_bytes(b"0123456789abcdef")
        profile = make_profile(tmp_path)
        rec = profile.state_dir / "fda_binary.json"
        rec.parent.mkdir(parents=True, exist_ok=True)
        rec.write_text(
            json.dumps({"path": str(tmp_path / "old_python"), "fda": True, "checked": "x"}),
            encoding="utf-8",
        )
        check = FullDiskAccessCheck(platform_name="darwin", binary=tmp_path / "new_python", tcc_db=tcc)
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "바뀌었다" in result.detail

    def test_기록_파일을_남긴다(self, tmp_path: Path) -> None:
        tcc = tmp_path / "TCC.db"
        tcc.write_bytes(b"0123456789abcdef")
        profile = make_profile(tmp_path)
        check = FullDiskAccessCheck(platform_name="darwin", binary=tmp_path / "python", tcc_db=tcc)
        check.run(PreflightContext(profile=profile))
        rec = profile.state_dir / "fda_binary.json"
        assert rec.is_file()
        data = json.loads(rec.read_text(encoding="utf-8"))
        assert data["path"] == str(tmp_path / "python")


# ScriptCapabilityGradeCheck — G40. 최상위 스크립트 전부 capabilities 등급
# 보유 점검의 이식. 등급 체계와 대상 디렉터리는 조직 설정이라 주입으로 받는다.


class TestScriptCapabilityGradeCheck:
    def test_설정이_없으면_통과한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        result = ScriptCapabilityGradeCheck().run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_등록부가_없으면_실패한다(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        profile = make_profile(tmp_path)
        check = ScriptCapabilityGradeCheck(
            scripts_dir=scripts_dir, capabilities_file=tmp_path / "capabilities.json"
        )
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is False
        assert result.fatal is True

    def test_등급_없는_스크립트를_찾아낸다(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "deploy.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        caps_file = tmp_path / "capabilities.json"
        caps_file.write_text(json.dumps({"capabilities": {}}), encoding="utf-8")
        profile = make_profile(tmp_path)
        check = ScriptCapabilityGradeCheck(scripts_dir=scripts_dir, capabilities_file=caps_file)
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "deploy.sh" in result.detail

    def test_잘못된_등급은_실패한다(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "deploy.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        caps_file = tmp_path / "capabilities.json"
        caps_file.write_text(
            json.dumps({"capabilities": {"deploy.sh": {"tier": "엉뚱"}}}), encoding="utf-8"
        )
        profile = make_profile(tmp_path)
        check = ScriptCapabilityGradeCheck(scripts_dir=scripts_dir, capabilities_file=caps_file)
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "엉뚱" in result.detail

    def test_모두_등급이_맞으면_통과한다(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "deploy.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        caps_file = tmp_path / "capabilities.json"
        caps_file.write_text(
            json.dumps({"capabilities": {"deploy.sh": {"tier": "owner"}}}), encoding="utf-8"
        )
        profile = make_profile(tmp_path)
        check = ScriptCapabilityGradeCheck(scripts_dir=scripts_dir, capabilities_file=caps_file)
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_외부_등재가_아니면_자리에_있어야_한다(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        caps_file = tmp_path / "capabilities.json"
        caps_file.write_text(
            json.dumps({"capabilities": {"ghost.sh": {"tier": "all"}}}), encoding="utf-8"
        )
        profile = make_profile(tmp_path)
        check = ScriptCapabilityGradeCheck(scripts_dir=scripts_dir, capabilities_file=caps_file)
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "ghost.sh" in result.detail

    def test_외부_등재는_자리_확인을_건너뛴다(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        caps_file = tmp_path / "capabilities.json"
        caps_file.write_text(
            json.dumps(
                {"capabilities": {"git": {"tier": "all", "external": True}}}
            ),
            encoding="utf-8",
        )
        profile = make_profile(tmp_path)
        check = ScriptCapabilityGradeCheck(scripts_dir=scripts_dir, capabilities_file=caps_file)
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is True


# SlackApiRegistrationCheck — G39. api_call() slack_api_methods.json 등재
# 점검의 이식. 호출부 파일 목록과 등록부 위치는 조직 설정이라 주입으로 받는다.


class TestSlackApiRegistrationCheck:
    def test_설정이_없으면_통과한다(self, tmp_path: Path) -> None:
        profile = make_profile(tmp_path)
        result = SlackApiRegistrationCheck().run(PreflightContext(profile=profile))
        assert result.ok is True

    def test_등록부가_없으면_실패한다(self, tmp_path: Path) -> None:
        src = tmp_path / "caller.py"
        src.write_text('api_call("chat.update")\n', encoding="utf-8")
        profile = make_profile(tmp_path)
        check = SlackApiRegistrationCheck(
            source_files=[src], registry_file=tmp_path / "slack_api_methods.json"
        )
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is False
        assert result.fatal is True

    def test_등록되지_않은_API_호출은_실패한다(self, tmp_path: Path) -> None:
        src = tmp_path / "caller.py"
        src.write_text('api_call("chat.update")\n', encoding="utf-8")
        reg = tmp_path / "slack_api_methods.json"
        reg.write_text(json.dumps({}), encoding="utf-8")
        profile = make_profile(tmp_path)
        check = SlackApiRegistrationCheck(source_files=[src], registry_file=reg)
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "chat.update" in result.detail

    def test_쓰지_않기로_한_API_호출은_실패한다(self, tmp_path: Path) -> None:
        src = tmp_path / "caller.py"
        src.write_text('api_call("channels.delete")\n', encoding="utf-8")
        reg = tmp_path / "slack_api_methods.json"
        reg.write_text(
            json.dumps({"_banned": {"channels.delete": {"이유": "위험"}}}), encoding="utf-8"
        )
        profile = make_profile(tmp_path)
        check = SlackApiRegistrationCheck(source_files=[src], registry_file=reg)
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "channels.delete" in result.detail
        assert "위험" in result.detail

    def test_문서나_실측이_비면_실패한다(self, tmp_path: Path) -> None:
        reg = tmp_path / "slack_api_methods.json"
        reg.write_text(
            json.dumps({"chat.update": {"doc": "", "verified": ""}}), encoding="utf-8"
        )
        profile = make_profile(tmp_path)
        check = SlackApiRegistrationCheck(source_files=[], registry_file=reg)
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is False
        assert "chat.update" in result.detail

    def test_등재되고_문서화된_호출은_통과한다(self, tmp_path: Path) -> None:
        src = tmp_path / "caller.py"
        src.write_text('api_call("chat.update")\n', encoding="utf-8")
        reg = tmp_path / "slack_api_methods.json"
        reg.write_text(
            json.dumps(
                {"chat.update": {"doc": "https://api.slack.com/methods/chat.update",
                                  "verified": "2026-01-01"}}
            ),
            encoding="utf-8",
        )
        profile = make_profile(tmp_path)
        check = SlackApiRegistrationCheck(source_files=[src], registry_file=reg)
        result = check.run(PreflightContext(profile=profile))
        assert result.ok is True


# 표준 점검 목록에 9종이 전부 들어 있는지 — sca-s5r 과 같은 이유.


class TestPreflightSuite확장:
    def test_표준_점검_목록에_9종이_전부_들어_있다(self) -> None:
        names = {c.name for c in PreflightSuite().checks}
        for cls in (
            PluginAstCheck,
            SkillSymlinkCheck,
            CodexConfigLoadCheck,
            McpScriptOwnershipCheck,
            FullDiskAccessCheck,
            PromptToolConsistencyCheck,
            DuplicateMcpSystemCheck,
            ScriptCapabilityGradeCheck,
            SlackApiRegistrationCheck,
        ):
            assert cls.name in names, cls.name

    def test_주입_인자가_실제_점검에_반영된다(self, tmp_path: Path) -> None:
        profile = make_profile(
            tmp_path, mcp_servers={"jira": {"command": "a"}, "atlassian": {"command": "b"}}
        )
        report = PreflightSuite(
            duplicate_mcp_groups={"jira": ("jira", "atlassian")}
        ).run(profile)
        assert any("jira" in r.detail and "atlassian" in r.detail for r in report.results)
