"""봇 메모리를 어느 경로에 쓰고, 엔진 쪽 경로를 어떻게 잇는지 검증한다.

2026-11-19 신지 — "기억해" 지시를 받은 턴이 Claude Code 가 알려 주는
`~/.claude/projects/<슬러그>/memory` 로 Write 를 불렀고, dontAsk 에서 경로에
`.claude` 가 들어가면 거부되므로(실측 2026-09-19) 저장이 안 됐다. 그 턴은
거부를 권한 문제로 보고했다.

그래서 쓰는 자리는 StatePaths.memory 하나로 두고, 작업 디렉터리마다 갈리는
엔진 쪽 경로를 그 자리를 가리키는 symlink 로 만든다. 경로는 한 봇의 페르소나
파일이 아니라 프롬프트가 알린다.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from slack_cli_agent.auth.principal import Principal, TrustLevel
from slack_cli_agent.config.paths import StatePaths
from slack_cli_agent.config.profile import Profile
from slack_cli_agent.engine.base import EngineRequest
from slack_cli_agent.engine.claude import ClaudeEngine
from slack_cli_agent.engine.transcript import ClaudeTranscriptReader, claude_project_slug
from slack_cli_agent.prompt.sections import CompositionContext, MemoryPathSection


def _profile(tmp_path: Path, home: Path) -> Profile:
    return Profile.from_dict({
        "name": "bot_a",
        "state_dir": str(tmp_path / "state"),
        "primary_engine": {
            "type": "claude",
            "binary": "/usr/bin/claude",
            "model": "test-model",
            # ClaudeEnvironmentPolicy 의 HOME_ENV_VAR 가 HOME 자체라 이 값이
            # CLI 가 실제로 쓰는 홈이다. 시험은 이 홈 아래를 본다.
            "home_dir": str(home),
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


def _engine(tmp_path: Path, home: Path) -> ClaudeEngine:
    profile = _profile(tmp_path, home)
    return ClaudeEngine(profile, profile.primary_engine)


def _engine_memory(home: Path, workdir: Path) -> Path:
    return home / ".claude" / "projects" / claude_project_slug(workdir) / "memory"


class TestMemoryStatePath:
    def test_memory_has_no_dot_claude_component(self, tmp_path):
        paths = StatePaths(tmp_path / "state")
        assert ".claude" not in paths.memory.parts, (
            "메모리를 쓰는 자리에 .claude 가 들어가면 dontAsk 에서 봇이 못 쓴다"
        )
        assert paths.memory_index == paths.memory / "MEMORY.md"

    def test_ensure_creates_memory(self, tmp_path):
        paths = StatePaths(tmp_path / "state")
        paths.ensure()
        assert paths.memory.is_dir()


class TestClaudeMemoryLink:
    def test_prepare_links_engine_path_to_state_memory(self, tmp_path):
        home = tmp_path / "home"
        engine = _engine(tmp_path, home)
        workdir = tmp_path / "work"
        workdir.mkdir()

        engine.prepare(_request(workdir))

        link = _engine_memory(home, workdir)
        assert link.is_symlink()
        assert link.resolve() == (tmp_path / "state" / "memory").resolve()

    def test_every_workdir_reads_the_same_memory(self, tmp_path):
        home = tmp_path / "home"
        engine = _engine(tmp_path, home)
        first, second = tmp_path / "work", tmp_path / "work" / "notes"
        second.mkdir(parents=True)

        engine.prepare(_request(first))
        engine.prepare(_request(second))
        (tmp_path / "state" / "memory" / "MEMORY.md").write_text("- 색인\n", encoding="utf-8")

        for workdir in (first, second):
            index = _engine_memory(home, workdir) / "MEMORY.md"
            assert index.read_text(encoding="utf-8") == "- 색인\n"

    def test_empty_engine_directory_is_replaced(self, tmp_path):
        home = tmp_path / "home"
        engine = _engine(tmp_path, home)
        workdir = tmp_path / "work"
        workdir.mkdir()
        _engine_memory(home, workdir).mkdir(parents=True)

        engine.prepare(_request(workdir))

        assert _engine_memory(home, workdir).is_symlink()

    def test_directory_with_files_is_left_alone(self, tmp_path):
        home = tmp_path / "home"
        engine = _engine(tmp_path, home)
        workdir = tmp_path / "work"
        workdir.mkdir()
        existing = _engine_memory(home, workdir)
        existing.mkdir(parents=True)
        (existing / "kept.md").write_text("지우면 안 되는 기록\n", encoding="utf-8")

        engine.prepare(_request(workdir))

        assert not existing.is_symlink()
        assert (existing / "kept.md").exists()

    def test_wrong_link_target_is_repointed(self, tmp_path):
        home = tmp_path / "home"
        engine = _engine(tmp_path, home)
        workdir = tmp_path / "work"
        workdir.mkdir()
        other = tmp_path / "other-memory"
        other.mkdir()
        link = _engine_memory(home, workdir)
        link.parent.mkdir(parents=True)
        link.symlink_to(other, target_is_directory=True)

        engine.prepare(_request(workdir))

        assert link.resolve() == (tmp_path / "state" / "memory").resolve()

    def test_transcript_and_memory_share_one_slug_rule(self, tmp_path):
        workdir = tmp_path / "work.dir"
        reader = ClaudeTranscriptReader(workdir, home=tmp_path / "home")
        transcript = reader.transcript_path("11111111-1111-1111-1111-111111111111")
        assert transcript.parent.name == claude_project_slug(workdir)


class TestMemoryPathSection:
    def _ctx(self, trust: TrustLevel, **kw: object) -> CompositionContext:
        principal = Principal(
            user_id="U-OWNER", channel="C1", trust=trust, is_direct_message=False,
        )
        return CompositionContext(principal=principal, **kw)  # type: ignore[arg-type]

    def _section(self, tmp_path: Path) -> MemoryPathSection:
        profile = _profile(tmp_path, tmp_path / "home")
        return MemoryPathSection(replace(profile, source_file=tmp_path / "bot_a.json"))

    def test_owner_turn_is_told_the_writable_path(self, tmp_path):
        section = self._section(tmp_path)
        ctx = self._ctx(TrustLevel.OWNER)
        assert section.applies_to(ctx)
        text = section.render(ctx)
        assert str(tmp_path / "state" / "memory") in text
        assert str(tmp_path / "state" / "memory" / "MEMORY.md") in text
        assert ".claude" in text

    def test_not_shown_to_non_owner(self, tmp_path):
        assert not self._section(tmp_path).applies_to(self._ctx(TrustLevel.GENERAL))

    def test_not_shown_on_aside_turn(self, tmp_path):
        section = self._section(tmp_path)
        assert not section.applies_to(self._ctx(TrustLevel.OWNER, postmortem=True))
