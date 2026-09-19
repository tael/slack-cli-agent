"""봇이 자기 스킬을 스스로 설치할 수 있는 자리를 검증한다.

2026-09-19 실측 — claude 는 `--permission-mode dontAsk` 에서 경로에 `.claude`
구성요소가 하나라도 있으면 Write·Bash 를 거부한다. `--add-dir` 로 그 상위를
넣어도 마찬가지다. 그런데 스킬 탐색 경로는 `<add-dir>/.claude/skills/` 라,
탐색 경로와 쓰기 가능 경로가 같으면 봇은 자기 스킬을 영원히 못 만든다.

그래서 쓰는 자리(skill_files)와 탐색 자리(.claude/skills)를 나누고, 후자를
전자를 가리키는 symlink 로 둔다. 같은 조건에서 발견과 쓰기가 모두 되는 것을
실행으로 확인했다.
"""

from __future__ import annotations

from pathlib import Path

from slack_cli_agent.config.paths import StatePaths
from slack_cli_agent.config.profile import Profile
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.engine.base import EngineRequest
from slack_cli_agent.engine.claude import ClaudeEngine


def _profile(tmp_path: Path) -> Profile:
    return Profile.from_dict({
        "name": "bot_a",
        "state_dir": str(tmp_path / "state"),
        "primary_engine": {
            "type": "claude", "binary": "/usr/bin/claude", "model": "test-model",
        },
        "owner_user_id": "U-OWNER",
        "troubleshoot_channel": "C-TROUBLE",
    })


def _request(workdir: Path) -> EngineRequest:
    return EngineRequest(
        prompt="p", system_prompt="s",
        session_id="11111111-1111-1111-1111-111111111111",
        resume=False, model="test-model", effort="medium", workdir=workdir,
    )


class TestSkillFilesPath:
    def test_skill_files_has_no_dot_claude_component(self, tmp_path):
        paths = StatePaths(tmp_path / "state")
        assert ".claude" not in paths.skill_files.parts, (
            "스킬을 쓰는 자리에 .claude 가 들어가면 dontAsk 에서 봇이 못 쓴다"
        )
        assert paths.skills in paths.skill_files.parents

    def test_ensure_creates_skill_files(self, tmp_path):
        paths = StatePaths(tmp_path / "state")
        paths.ensure()
        assert paths.skill_files.is_dir()


class TestClaudeSkillDiscovery:
    def test_prepare_links_discovery_path_to_skill_files(self, tmp_path):
        profile = _profile(tmp_path)
        engine = ClaudeEngine(profile, RuntimeSettings())

        engine.prepare(_request(tmp_path / "workdir"))

        link = profile.paths.skills / ".claude" / "skills"
        assert link.is_symlink(), "탐색 경로가 symlink 로 만들어지지 않았다"
        assert link.resolve() == profile.paths.skill_files.resolve()

    def test_prepare_keeps_an_existing_real_directory(self, tmp_path):
        profile = _profile(tmp_path)
        existing = profile.paths.skills / ".claude" / "skills"
        existing.mkdir(parents=True)
        (existing / "keep.md").write_text("남아 있어야 한다", encoding="utf-8")

        ClaudeEngine(profile, RuntimeSettings()).prepare(_request(tmp_path / "workdir"))

        assert (existing / "keep.md").exists(), "이미 있던 스킬 파일을 지웠다"

    def test_command_adds_the_skills_root_after_prepare(self, tmp_path):
        profile = _profile(tmp_path)
        engine = ClaudeEngine(profile, RuntimeSettings())
        request = _request(tmp_path / "workdir")

        engine.prepare(request)
        cmd = engine.build_command(request)

        assert "--add-dir" in cmd
        assert str(profile.paths.skills) in cmd, "스킬 경로가 실행 인자에 없다"
