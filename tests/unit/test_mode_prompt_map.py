"""모드→프롬프트 매핑과 상시 스킬 적재 — B8 (I2/I20/I83).

채널의 mode 가 어떤 프롬프트 파일로 조립되는지, 그리고 trigger 낱말과 무관하게
늘 실리는 공통 지식(스킬) 파일이 실제로 늘 실리는지를 본다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from slack_cli_agent.auth.principal import Principal, TrustLevel
from slack_cli_agent.core.application import (
    ALWAYS_SKILLS,
    DEFAULT_PROMPT,
    KNOWN_MODES,
    mode_prompt_names,
)
from slack_cli_agent.prompt.knowledge import KnowledgeLoader
from slack_cli_agent.prompt.library import PromptLibrary
from slack_cli_agent.prompt.sections import ChannelModeSection, CompositionContext

PRINCIPAL = Principal(user_id="U1", channel="C1", trust=TrustLevel.GENERAL, is_direct_message=False)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TestKNOWN_MODES:
    def test_chat_모드가_포함된다(self) -> None:
        assert "chat" in KNOWN_MODES

    def test_기존_모드도_그대로_있다(self) -> None:
        assert {"private", "agent_coach", "api_helpdesk"} <= set(KNOWN_MODES)

    def test_각_모드가_PROMPT_이름으로_매핑된다(self) -> None:
        names = mode_prompt_names()
        assert names["chat"] == "PROMPT_CHAT"
        assert names["private"] == "PROMPT_PRIVATE"
        assert names["agent_coach"] == "PROMPT_AGENT_COACH"
        assert names["api_helpdesk"] == "PROMPT_API_HELPDESK"


class Test패키지_기본_프롬프트:
    """mode_prompt_names() 가 가리키는 이름은 패키지 동봉 자산에 실존해야 한다
    (state 디렉터리가 비어 있어도 MissingPromptError 없이 떨어진다)."""

    def test_모든_모드가_기본_자산을_갖는다(self, tmp_path: Path) -> None:
        empty_state = tmp_path / "state_prompts"
        empty_state.mkdir()
        library = PromptLibrary(empty_state)
        for mode, prompt_name in mode_prompt_names().items():
            text = library.text(prompt_name)
            assert text.strip(), f"{mode} -> {prompt_name} 본문이 비어 있다"

    def test_DEFAULT_PROMPT도_실존한다(self, tmp_path: Path) -> None:
        empty_state = tmp_path / "state_prompts"
        empty_state.mkdir()
        library = PromptLibrary(empty_state)
        assert library.text(DEFAULT_PROMPT).strip()


class TestChannelModeSection_매핑:
    """각 모드가 실제로 그 모드 전용 조각으로 조립되는지 — chat 포함."""

    @pytest.fixture
    def prompts_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "prompts"
        for name, body in {
            "prompt_private": "사적 채널 조각",
            "prompt_agent_coach": "코치 채널 조각",
            "prompt_api_helpdesk": "헬프데스크 채널 조각",
            "prompt_chat": "잡담 채널 조각",
            "prompt_default": "기본 채널 조각",
        }.items():
            write(d / f"{name}.md", body)
        return d

    @pytest.fixture
    def library(self, prompts_dir: Path) -> PromptLibrary:
        return PromptLibrary(prompts_dir)

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [
            ("private", "사적 채널 조각"),
            ("agent_coach", "코치 채널 조각"),
            ("api_helpdesk", "헬프데스크 채널 조각"),
            ("chat", "잡담 채널 조각"),
        ],
    )
    def test_모드별로_기대한_조각을_고른다(
        self, library: PromptLibrary, mode: str, expected: str
    ) -> None:
        section = ChannelModeSection(mode_prompt_names(), default_prompt=DEFAULT_PROMPT)
        ctx = CompositionContext(principal=PRINCIPAL, channel_mode=mode, library=library)
        assert section.render(ctx) == expected

    def test_모르는_모드는_기본_조각으로_떨어진다(self, library: PromptLibrary) -> None:
        section = ChannelModeSection(mode_prompt_names(), default_prompt=DEFAULT_PROMPT)
        ctx = CompositionContext(principal=PRINCIPAL, channel_mode="없는모드", library=library)
        assert section.render(ctx) == "기본 채널 조각"


class TestALWAYS_SKILLS_기본값:
    def test_기본값은_빈_튜플이다(self) -> None:
        """조직 특수 스킬 이름은 리포에 심지 않는다 — 공통골격만 둔다."""
        assert ALWAYS_SKILLS == ()


class TestKnowledgeLoader_상시스킬:
    """trigger 낱말이 안 맞아도 always_skills 에 든 이름은 늘 실린다."""

    @pytest.fixture
    def persona_file(self, tmp_path: Path) -> Path:
        p = tmp_path / "persona" / "PERSONA.md"
        write(p, "페르소나")
        return p

    @pytest.fixture
    def knowledge_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "persona" / "knowledge"
        d.mkdir(parents=True)
        write(d / "_metabase.md", "<!-- when: metabase,메타베이스 -->\n메타베이스 스킬 지식")
        return d

    def test_트리거가_안_맞으면_기본은_빠진다(
        self, persona_file: Path, knowledge_dir: Path
    ) -> None:
        loader = KnowledgeLoader(persona_file, knowledge_dir)
        text = loader.knowledge_text("chan", "오늘 날씨 어때")
        assert "메타베이스 스킬 지식" not in text

    def test_always_skills에_들어있으면_트리거와_무관하게_실린다(
        self, persona_file: Path, knowledge_dir: Path
    ) -> None:
        loader = KnowledgeLoader(persona_file, knowledge_dir, always_skills=("metabase",))
        text = loader.knowledge_text("chan", "오늘 날씨 어때")
        assert "메타베이스 스킬 지식" in text

    def test_always_skills에_없으면_여전히_트리거를_따른다(
        self, persona_file: Path, knowledge_dir: Path
    ) -> None:
        loader = KnowledgeLoader(persona_file, knowledge_dir, always_skills=("다른스킬",))
        text = loader.knowledge_text("chan", "오늘 날씨 어때")
        assert "메타베이스 스킬 지식" not in text

    def test_트리거가_맞으면_always_skills와_무관하게_실린다(
        self, persona_file: Path, knowledge_dir: Path
    ) -> None:
        loader = KnowledgeLoader(persona_file, knowledge_dir)
        text = loader.knowledge_text("chan", "메타베이스 조회해줘")
        assert "메타베이스 스킬 지식" in text



class TestChannelModeSection_잡담_활성:
    """chat=active 채널은 mode 와 무관하게 잡담 조각을 쓴다(마메치 bot.py
    prompt_for 2026-10-16). mode 만 private 로 남아 존댓말 조각이 골라졌다."""

    @pytest.fixture
    def library(self, tmp_path: Path) -> PromptLibrary:
        d = tmp_path / "prompts"
        write(d / "prompt_private.md", "사적 채널 조각")
        write(d / "prompt_chat.md", "잡담 채널 조각")
        write(d / "prompt_default.md", "기본 채널 조각")
        return PromptLibrary(d)

    def test_active_면_잡담_조각이_이긴다(self, library: PromptLibrary) -> None:
        section = ChannelModeSection(mode_prompt_names(), default_prompt=DEFAULT_PROMPT)
        ctx = CompositionContext(
            principal=PRINCIPAL, channel_mode="private", chat_level="active", library=library,
        )
        assert section.render(ctx) == "잡담 채널 조각"

    @pytest.mark.parametrize("level", ["normal", "quiet"])
    def test_active_가_아니면_mode_를_따른다(self, library: PromptLibrary, level: str) -> None:
        section = ChannelModeSection(mode_prompt_names(), default_prompt=DEFAULT_PROMPT)
        ctx = CompositionContext(
            principal=PRINCIPAL, channel_mode="private", chat_level=level, library=library,
        )
        assert section.render(ctx) == "사적 채널 조각"
