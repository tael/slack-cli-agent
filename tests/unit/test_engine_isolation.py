"""봇 A 실행 시 봇 B 의 MCP 서버·스킬·홈 경로가 새지 않는 것을 검증한다.

관찰 지점은 실제로 만들어지는 것 셋이다 — build_command() 가 만드는 인자
리스트, EngineEnvironmentPolicy.build() 가 만드는 환경 변수 dict, 엔진이
prepare() 에서 실제로 쓰는 설정 파일(agy 의 settings.json). "안 새는 것"을
부작용의 부재로만 보면 검출력이 0 이 되므로, 봇 A 와 봇 B 에 서로 다른
MCP 서버 이름·명령·환경변수·홈 경로를 주고 봇 A 를 실행한 산출물 안에 봇 B
쪽 고유 문자열이 하나도 없는 것을 직접 문자열 검색으로 확인한다.

MCP 서버 인자 주입(sca-kos.2)과 스킬 디렉터리 격리(sca-kos.3)는 이 시점에
구현되어 있지 않다 — 그 관찰 지점은 xfail(strict=True) 로 남겨, 구현되는
순간 예상외 성공(XPASS)으로 실패하게 해서 마커 제거를 강제한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from slack_cli_agent.config.profile import Profile
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.engine.base import Engine, EngineRequest
from slack_cli_agent.engine.claude import ClaudeEngine
from slack_cli_agent.engine.codex import CodexEngine
from slack_cli_agent.engine.environment import create_environment_policy
from slack_cli_agent.engine.gemini import GeminiEngine

_ENGINE_CLASSES: Mapping[str, type[Engine]] = {
    "claude": ClaudeEngine,
    "codex": CodexEngine,
    "gemini": GeminiEngine,
}

_ENGINE_NAMES = tuple(_ENGINE_CLASSES)


def _make_profile(
    bot_name: str, engine_type: str, home_dir: Path,
) -> Profile:
    """봇 하나의 프로필. MCP 서버 이름·명령·env 값이 전부 그 봇 고유 문자열을 담는다."""
    mcp_name = f"mcp-{bot_name}-only"
    return Profile.from_dict({
        "name": bot_name,
        "primary_engine": {
            "type": engine_type,
            "binary": f"/usr/bin/{engine_type}",
            "model": "test-model",
            "home_dir": str(home_dir),
        },
        "owner_user_id": "U-OWNER",
        "troubleshoot_channel": "C-TROUBLE",
        "mcp_servers": {
            mcp_name: {
                "command": f"run-{bot_name}-mcp-server",
                "args": [f"--secret-for-{bot_name}"],
                "env": {f"{bot_name.upper()}_MCP_TOKEN": f"token-of-{bot_name}"},
            },
        },
    })


def _make_request(workdir: Path) -> EngineRequest:
    return EngineRequest(
        prompt="테스트 프롬프트",
        system_prompt="테스트 시스템 프롬프트",
        session_id="11111111-1111-1111-1111-111111111111",
        resume=False,
        model="test-model",
        effort="medium",
        workdir=workdir,
    )


def _build_command_text(engine_type: str, profile: Profile, workdir: Path) -> str:
    engine = _ENGINE_CLASSES[engine_type](profile, RuntimeSettings())
    request = _make_request(workdir)
    engine.prepare(request)
    return " ".join(engine.build_command(request))


def _build_env_text(engine_type: str, profile: Profile, source_env: Mapping[str, str]) -> str:
    policy = create_environment_policy(
        engine_type, profile_name=profile.name, home_dir=profile.primary_engine.home_dir,
    )
    env = policy.build(source_env)
    return " ".join(f"{k}={v}" for k, v in env.items())


def _bot_b_markers(bot_b_name: str, home_dir_b: Path) -> list[str]:
    return [
        f"mcp-{bot_b_name}-only",
        f"run-{bot_b_name}-mcp-server",
        f"--secret-for-{bot_b_name}",
        f"{bot_b_name.upper()}_MCP_TOKEN",
        f"token-of-{bot_b_name}",
        str(home_dir_b),
    ]


class TestCommandLineIsolation:
    """build_command() 산출물에 다른 봇의 고유 문자열이 없는 것을 본다."""

    @pytest.mark.parametrize("engine_type", _ENGINE_NAMES)
    def test_bot_a_command_has_no_bot_b_markers(self, tmp_path, engine_type):
        home_a = tmp_path / "bot-a-home"
        home_b = tmp_path / "bot-b-home"
        profile_a = _make_profile("bot_a", engine_type, home_a)

        cmd_text = _build_command_text(engine_type, profile_a, tmp_path / "workdir")

        for marker in _bot_b_markers("bot_b", home_b):
            assert marker not in cmd_text, (
                f"{engine_type} build_command 에 봇 B 의 고유 문자열 {marker!r} 이 들어갔다"
            )


class TestEnvironmentIsolation:
    """EngineEnvironmentPolicy.build() 산출물에 다른 봇의 고유 문자열이 없는 것을 본다."""

    @pytest.mark.parametrize("engine_type", _ENGINE_NAMES)
    def test_bot_a_env_has_no_bot_b_markers(self, tmp_path, engine_type):
        home_a = tmp_path / "bot-a-home"
        home_b = tmp_path / "bot-b-home"
        profile_a = _make_profile("bot_a", engine_type, home_a)

        # 부모 프로세스 환경에 봇 B 몫 값이 실제로 섞여 있는 상황을 흉내낸다 —
        # 이 값들이 봇 A 실행에 새면 격리가 깨진 것이다.
        polluted_source_env = {
            "PATH": "/usr/bin:/bin",
            "LANG": "ko_KR.UTF-8",
            "HOME": str(home_b),
            "CODEX_HOME": str(home_b / ".codex"),
            "BOT_B_MCP_TOKEN": "token-of-bot_b",
            "ANTHROPIC_API_KEY": "sk-ant-should-be-stripped",
        }

        env_text = _build_env_text(engine_type, profile_a, polluted_source_env)

        assert "token-of-bot_b" not in env_text
        assert "BOT_B_MCP_TOKEN" not in env_text
        assert "sk-ant-should-be-stripped" not in env_text

    @pytest.mark.parametrize("engine_type", ("codex", "gemini"))
    def test_bot_a_env_home_is_not_bot_b_home(self, tmp_path, engine_type):
        """codex(CODEX_HOME)·gemini(HOME) 는 홈 경로 환경변수를 프로필 값으로
        덮어쓰는 격리 지점이 있다 — claude 는 아직 없어서(아래 xfail) 따로 뺐다."""
        home_a = tmp_path / "bot-a-home"
        home_b = tmp_path / "bot-b-home"
        profile_a = _make_profile("bot_a", engine_type, home_a)
        polluted_source_env = {
            "PATH": "/usr/bin:/bin",
            "LANG": "ko_KR.UTF-8",
            "HOME": str(home_b),
            "CODEX_HOME": str(home_b / ".codex"),
        }

        env_text = _build_env_text(engine_type, profile_a, polluted_source_env)

        assert str(home_b) not in env_text

    @pytest.mark.xfail(
        strict=True,
        reason="claude 엔진은 봇별 HOME 격리 개념이 아직 없다 — 부모 프로세스의 HOME 을 그대로 넘긴다",
    )
    def test_claude_env_home_is_not_bot_b_home(self, tmp_path):
        home_a = tmp_path / "bot-a-home"
        home_b = tmp_path / "bot-b-home"
        profile_a = _make_profile("bot_a", "claude", home_a)
        polluted_source_env = {
            "PATH": "/usr/bin:/bin",
            "LANG": "ko_KR.UTF-8",
            "HOME": str(home_b),
        }

        env_text = _build_env_text("claude", profile_a, polluted_source_env)

        assert str(home_b) not in env_text


class TestGeminiSettingsFileIsolation:
    """agy 는 prepare() 에서 settings.json 을 실제로 쓴다 — 그 파일이 관찰 지점이다."""

    def test_bot_a_prepare_does_not_touch_bot_b_home(self, tmp_path):
        home_a = tmp_path / "bot-a-home"
        home_b = tmp_path / "bot-b-home"
        home_b.mkdir()
        profile_a = _make_profile("bot_a", "gemini", home_a)
        engine = GeminiEngine(profile_a, RuntimeSettings())

        engine.prepare(_make_request(tmp_path / "workdir"))

        settings_a = home_a / ".gemini" / "antigravity-cli" / "settings.json"
        settings_b = home_b / ".gemini" / "antigravity-cli" / "settings.json"
        assert settings_a.exists()
        assert not settings_b.exists()


class TestMcpInjectionIsolation:
    """MCP 서버가 실제로 명령줄·설정 파일에 실리는 것과, 봇별로 갈리는 것 둘 다를 본다.

    claude(--mcp-config 에 인라인 JSON 문자열)와 codex(-c mcp_servers.<이름>.* 오버라이드)는
    build_command() 인자 목록에 서버 값이 그대로 나타나 이 관찰 지점으로 잡힌다(sca-kos.2).

    gemini(agy)는 다르다 — agy --help 로 확인한 실제 플래그 목록에 인라인 MCP 설정을
    받는 자리가 없다. 유일한 경로는 docs/agy-실측.md 가 이미 확인해 둔
    작업공간 로컬 파일(<work_root>/.agents/mcp_config.json) 뿐이고, 그 파일 경로만
    build_command() 에 실리지 내용(서버 이름·명령)은 안 실린다. 그래서 이 관찰
    지점(명령줄 텍스트)으로는 gemini 의 주입 여부를 볼 수 없어 xfail 로 남긴다 —
    추측으로 새 플래그를 지어내지 않는다.
    """

    @pytest.mark.parametrize(
        "engine_type",
        [
            "claude",
            "codex",
            pytest.param(
                "gemini",
                marks=pytest.mark.xfail(
                    strict=True,
                    reason="agy 는 인라인 MCP 설정 플래그가 없다(agy --help 로 확인) — "
                    "workspace-local mcp_config.json 파일로만 주입되어 이 시험의 "
                    "관찰 지점(build_command 인자 목록)에는 안 나타난다",
                ),
            ),
        ],
    )
    def test_bot_a_mcp_server_appears_and_bot_b_does_not(self, tmp_path, engine_type):
        home_a = tmp_path / "bot-a-home"
        home_b = tmp_path / "bot-b-home"
        profile_a = _make_profile("bot_a", engine_type, home_a)

        cmd_text = _build_command_text(engine_type, profile_a, tmp_path / "workdir")

        assert "run-bot_a-mcp-server" in cmd_text, "봇 A 자신의 MCP 서버 명령이 어디에도 없다"
        for marker in _bot_b_markers("bot_b", home_b):
            assert marker not in cmd_text


class TestSkillDirectoryIsolation:
    """봇마다 상태 디렉터리 아래 skills/ 를 두고 그 경로만 보이는 것을 본다(설계는 sca-kos.3).

    claude 는 공식 문서(code.claude.com/docs/en/skills)가 명시한 경로다 —
    "--add-dir 로 추가한 디렉터리의 .claude/skills/ 에서 스킬을 읽는다"고 인용돼
    있어 build_command() 에 --add-dir <skills 경로> 를 추가하면 된다.

    codex 와 gemini(agy)는 다르다 — 공식 문서 모두 스킬 탐색 경로를
    작업 디렉터리(CWD)나 리포지토리 루트, 또는 HOME 기준 고정 관례
    (`.agents/skills`)로만 밝히고, 임의 경로를 지정하는 CLI 플래그를 안 낸다.
    상태 디렉터리 아래 skills/ 를 그 관례 경로에 배치하는 것은 prepare() 의
    파일 배치(symlink/copy)로나 가능한데, 그러면 이 시험의 관찰 지점인
    build_command() 인자 목록에는 안 나타난다. 추측으로 없는 플래그를
    지어내지 않고 xfail 로 남긴다.
    """

    @pytest.mark.parametrize(
        "engine_type",
        [
            "claude",
            pytest.param(
                "codex",
                marks=pytest.mark.xfail(
                    strict=True,
                    reason="codex 는 .agents/skills 를 CWD·리포지토리 루트·HOME 기준으로만 "
                    "찾는다(공식 문서 확인) — 임의 경로를 가리키는 플래그가 없어 "
                    "build_command() 인자 목록에 상태 디렉터리 경로가 실릴 수 없다",
                ),
            ),
            pytest.param(
                "gemini",
                marks=pytest.mark.xfail(
                    strict=True,
                    reason="agy 는 <work_root>/.agents/skills/ 고정 관례로만 스킬을 찾는다"
                    "(docs/agy-실측.md 확인) — 임의 경로를 가리키는 플래그가 없어 "
                    "build_command() 인자 목록에 상태 디렉터리 경로가 실릴 수 없다",
                ),
            ),
        ],
    )
    def test_bot_a_sees_only_its_own_skills_directory(self, tmp_path, engine_type):
        home_a = tmp_path / "bot-a-home"
        profile_a = _make_profile("bot_a", engine_type, home_a)

        assert hasattr(profile_a.paths, "skills"), "봇별 skills 디렉터리 개념이 아직 없다"
        skills_dir_a = profile_a.paths.skills
        skills_dir_a.mkdir(parents=True, exist_ok=True)
        (skills_dir_a / "skill-of-bot_a.md").write_text("bot_a 전용 스킬", encoding="utf-8")

        cmd_text = _build_command_text(engine_type, profile_a, tmp_path / "workdir")

        assert str(skills_dir_a) in cmd_text, "봇 자신의 skills 경로가 실행 인자 어디에도 없다"
        assert str(Path.home() / ".claude" / "skills") not in cmd_text
        assert str(Path.home() / ".gemini" / "config" / "skills") not in cmd_text
