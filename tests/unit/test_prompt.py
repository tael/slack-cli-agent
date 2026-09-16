"""프롬프트 조립 — PromptLibrary, KnowledgeLoader, SystemPromptComposer."""

from __future__ import annotations

from pathlib import Path

import pytest

from slack_cli_agent.auth.principal import Principal, TrustLevel
from slack_cli_agent.core.errors import MissingPromptError
from slack_cli_agent.prompt.composer import SystemPromptComposer
from slack_cli_agent.prompt.knowledge import KnowledgeLoader
from slack_cli_agent.prompt.library import PromptLibrary
from slack_cli_agent.prompt.sections import (
    AskerSection,
    AuthoritySection,
    ChannelModeSection,
    CompositionContext,
    KnowledgeSection,
    OwnerNoteSection,
    PersonaSection,
    ReviewFormatSection,
    SensitiveGuardSection,
    SilenceRuleSection,
    SlackFormatSection,
    TrustedSection,
    WatchSection,
)

OWNER = Principal(user_id="U_OWNER", channel="D1", trust=TrustLevel.OWNER, is_direct_message=True)
OWNER_PUBLIC = Principal(
    user_id="U_OWNER", channel="C1", trust=TrustLevel.OWNER, is_direct_message=False
)
STRANGER = Principal(
    user_id="U_X", channel="C1", trust=TrustLevel.GENERAL, is_direct_message=False
)
TRUSTED = Principal(
    user_id="U_T", channel="C1", trust=TrustLevel.TRUSTED, is_direct_message=False
)

MODE_PROMPTS = {"private": "PROMPT_PRIVATE"}


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def prompts_dir(tmp_path: Path) -> Path:
    d = tmp_path / "prompts"
    d.mkdir()
    for name, body in {
        "prompt_private": "채널 프롬프트 <<NEVER_DISCLOSE>> <<SLACK_FORMAT>>",
        "prompt_default": "기본 채널 프롬프트",
        "owner_note": "소유자 안내",
        "non_owner_note": "비소유자 안내",
        "slack_format_rich": "리치 서식",
        "slack_format_plain": "평문 서식",
        "postmortem_note": "부검 안내",
        "debug_trace_note": "디버그 추적 안내",
        "format_review_note": "서식 점검 안내",
        "direction_note": "\n방향 안내",
        "trusted_note": "신뢰 안내",
        "sensitive_guard": "민감정보 가드",
        "full_authority_note": "최고권한 안내",
        "mechanism_note": "구조공개 안내",
        "watch_note": "지켜보기 안내",
        "watch_check_note": "확인만 하는 안내",
        "chat_guide_normal": "보통 채널 안내",
    }.items():
        write(d / f"{name}.md", body)
    return d


@pytest.fixture
def library(prompts_dir: Path) -> PromptLibrary:
    return PromptLibrary(prompts_dir, placeholders={"NEVER_DISCLOSE": "[절대금지]"})


@pytest.fixture
def knowledge(tmp_path: Path) -> KnowledgeLoader:
    persona_dir = tmp_path / "persona"
    persona_dir.mkdir()
    knowledge_dir = persona_dir / "knowledge"
    knowledge_dir.mkdir()
    persona_file = persona_dir / "PERSONA.md"
    write(persona_file, "페르소나 본문")
    return KnowledgeLoader(persona_file, knowledge_dir)


def base_sections() -> list:
    return [
        PersonaSection(),
        KnowledgeSection(),
        ChannelModeSection(MODE_PROMPTS, default_prompt="PROMPT_DEFAULT"),
        OwnerNoteSection(),
        SlackFormatSection(),
        ReviewFormatSection(),
        AskerSection(),
        TrustedSection(),
        SensitiveGuardSection(),
        AuthoritySection(),
        WatchSection(),
        SilenceRuleSection(),
    ]


class TestPromptLibrary:
    def test_파일이_없으면_MissingPromptError(self, library: PromptLibrary) -> None:
        with pytest.raises(MissingPromptError):
            library.text("NOT_EXIST")

    def test_파일이_비어있으면_MissingPromptError(self, prompts_dir: Path) -> None:
        write(prompts_dir / "empty.md", "   \n  ")
        library = PromptLibrary(prompts_dir)
        with pytest.raises(MissingPromptError):
            library.text("EMPTY")

    def test_자리표를_채운다(self, library: PromptLibrary) -> None:
        text = library.text("PROMPT_PRIVATE")
        assert text == "채널 프롬프트 [절대금지] <<SLACK_FORMAT>>"

    def test_keep_slots에_있으면_치환하지_않는다(self, library: PromptLibrary) -> None:
        text = library.text("PROMPT_PRIVATE", keep_slots=("NEVER_DISCLOSE",))
        assert text == "채널 프롬프트 <<NEVER_DISCLOSE>> <<SLACK_FORMAT>>"


class TestKnowledgeLoader:
    def test_페르소나_파일이_없으면_빈_문자열(self, tmp_path: Path) -> None:
        loader = KnowledgeLoader(tmp_path / "없음.md", tmp_path / "knowledge")
        assert loader.persona_text() == ""

    def test_페르소나_본문을_읽는다(self, knowledge: KnowledgeLoader) -> None:
        assert knowledge.persona_text() == "페르소나 본문"

    def test_도메인_파일은_include_domain일때만_붙는다(self, tmp_path: Path) -> None:
        persona_file = tmp_path / "PERSONA.md"
        write(persona_file, "페르소나")
        domain_file = tmp_path / "DOMAIN.md"
        write(domain_file, "도메인 사실")
        loader = KnowledgeLoader(persona_file, tmp_path / "knowledge", domain_file)
        assert loader.persona_text(include_domain=False) == "페르소나"
        assert loader.persona_text(include_domain=True) == "페르소나\n\n도메인 사실"

    def test_when_표식이_없으면_늘_실린다(self, knowledge: KnowledgeLoader, tmp_path: Path) -> None:
        write(tmp_path / "persona" / "knowledge" / "_어조.md", "늘 싣는 지식")
        text = knowledge.knowledge_text("chan", "아무 말")
        assert "늘 싣는 지식" in text

    def test_when_표식의_낱말이_본문에_있어야_싣는다(
        self, knowledge: KnowledgeLoader, tmp_path: Path
    ) -> None:
        write(
            tmp_path / "persona" / "knowledge" / "_배포.md",
            "<!-- when: deploy,pipeline -->\n배포 지식",
        )
        assert "배포 지식" not in knowledge.knowledge_text("chan", "오늘 날씨")
        assert "배포 지식" in knowledge.knowledge_text("chan", "pipeline 상태 보여줘")

    def test_싣지_않은_파일은_이름을_남긴다(
        self, knowledge: KnowledgeLoader, tmp_path: Path
    ) -> None:
        write(
            tmp_path / "persona" / "knowledge" / "_배포.md",
            "<!-- when: deploy -->\n내용",
        )
        text = knowledge.knowledge_text("chan", "무관한 말")
        assert "배포" in text
        assert "지금 싣지 않은 주제별 지식" in text

    def test_채널별_지식이_있으면_붙는다(self, knowledge: KnowledgeLoader, tmp_path: Path) -> None:
        write(tmp_path / "persona" / "knowledge" / "chan.md", "이 채널만의 지식")
        text = knowledge.knowledge_text("chan", "")
        assert "이 채널만의 지식" in text
        assert "이 채널의 지식" in text


class TestSystemPromptComposer:
    def _compose(self, library, knowledge, principal, **kwargs) -> str:
        composer = SystemPromptComposer(library, knowledge, base_sections())
        ctx = CompositionContext(principal=principal, channel_mode="private", **kwargs)
        return composer.compose(ctx)

    def test_일반_사용자는_NEVER_DISCLOSE가_치환된_채로_남는다(
        self, library: PromptLibrary, knowledge: KnowledgeLoader
    ) -> None:
        text = self._compose(library, knowledge, STRANGER)
        assert "[절대금지]" in text
        assert "<<NEVER_DISCLOSE>>" not in text

    def test_최고권한_자리에서는_NEVER_DISCLOSE가_지워진다(
        self, library: PromptLibrary, knowledge: KnowledgeLoader
    ) -> None:
        text = self._compose(library, knowledge, OWNER)
        assert "[절대금지]" not in text
        assert "<<NEVER_DISCLOSE>>" not in text
        assert "최고권한 안내" in text

    def test_최고권한이_아니면_민감정보_가드가_붙는다(
        self, library: PromptLibrary, knowledge: KnowledgeLoader
    ) -> None:
        text = self._compose(library, knowledge, STRANGER)
        assert "민감정보 가드" in text

    def test_소유자에게는_민감정보_가드가_안_붙는다(
        self, library: PromptLibrary, knowledge: KnowledgeLoader
    ) -> None:
        text = self._compose(library, knowledge, OWNER_PUBLIC)
        assert "민감정보 가드" not in text

    def test_신뢰_사용자는_신뢰_안내가_붙는다(
        self, library: PromptLibrary, knowledge: KnowledgeLoader
    ) -> None:
        text = self._compose(library, knowledge, TRUSTED)
        assert "신뢰 안내" in text

    def test_일반_사용자는_신뢰_안내가_안_붙는다(
        self, library: PromptLibrary, knowledge: KnowledgeLoader
    ) -> None:
        text = self._compose(library, knowledge, STRANGER)
        assert "신뢰 안내" not in text

    def test_리치_채널은_리치_서식이_들어간다(
        self, library: PromptLibrary, knowledge: KnowledgeLoader
    ) -> None:
        text = self._compose(library, knowledge, STRANGER, is_rich=True)
        assert "리치 서식" in text
        assert "평문 서식" not in text

    def test_평문_채널은_평문_서식이_들어간다(
        self, library: PromptLibrary, knowledge: KnowledgeLoader
    ) -> None:
        text = self._compose(library, knowledge, STRANGER, is_rich=False)
        assert "평문 서식" in text

    def test_부검_자리는_watch_안내가_안_붙는다(
        self, library: PromptLibrary, knowledge: KnowledgeLoader
    ) -> None:
        text = self._compose(library, knowledge, STRANGER, postmortem=True)
        assert "부검 안내" in text
        assert "지켜보기 안내" not in text

    def test_평소_대화는_watch_안내가_붙는다(
        self, library: PromptLibrary, knowledge: KnowledgeLoader
    ) -> None:
        text = self._compose(library, knowledge, STRANGER)
        assert "지켜보기 안내" in text

    def test_부르지_않았으면_침묵_규칙이_붙는다(
        self, library: PromptLibrary, knowledge: KnowledgeLoader
    ) -> None:
        text = self._compose(library, knowledge, STRANGER, unaddressed=True)
        assert "[침묵]" in text
        assert "보통 채널 안내" in text

    def test_조립_순서는_원본과_같다(
        self, library: PromptLibrary, knowledge: KnowledgeLoader
    ) -> None:
        """페르소나 -> 채널 지식 -> 채널모드 -> 화자안내 -> 신뢰 -> 최고권한 -> 지켜보기."""
        text = self._compose(library, knowledge, OWNER, asker_name="누군가")
        assert (
            text.index("페르소나 본문")
            < text.index("채널 프롬프트")
            < text.index("최고권한 안내")
            < text.index("지켜보기 안내")
        )


class TestPresentPeopleSection:
    """원본 present_note() 이식 — 함께 있는 사람 알림.

    원본은 사람이 둘뿐이면(화자 1인 이하) 적지 않는다. 셋 이상일 때만
    다른 사람을 없는 사람처럼 말할 여지가 생기기 때문이다.
    """

    def test_사람이_둘_미만이면_붙지_않는다(self) -> None:
        from slack_cli_agent.prompt.sections import CompositionContext, PresentPeopleSection

        section = PresentPeopleSection()
        ctx = CompositionContext(principal=STRANGER, people=(("김서준", "<@U1>"),))
        assert section.applies_to(ctx) is False

    def test_사람이_둘_이상이면_붙는다(self) -> None:
        from slack_cli_agent.prompt.sections import CompositionContext, PresentPeopleSection

        section = PresentPeopleSection()
        ctx = CompositionContext(
            principal=STRANGER,
            people=(("김서준", "<@U1>"), ("김영희", "<@U2>")),
        )
        assert section.applies_to(ctx) is True
        text = section.render(ctx)
        assert "이 대화에 함께 있는 사람이다" in text
        assert "- 김서준 <@U1>" in text
        assert "- 김영희 <@U2>" in text
        assert "여기 있는 사람을 없는 사람처럼 말하지 않는다." in text

    def test_말을_건_사람에게는_표시가_붙는다(self) -> None:
        from slack_cli_agent.prompt.sections import CompositionContext, PresentPeopleSection

        section = PresentPeopleSection()
        ctx = CompositionContext(
            principal=STRANGER,
            asker_id="U1",
            people=(("김서준", "<@U1>"), ("김영희", "<@U2>")),
        )
        text = section.render(ctx)
        assert "- 김서준 <@U1>  (지금 말을 건 사람)" in text
        assert "- 김영희 <@U2>  (지금 말을 건 사람)" not in text


class TestPromptLibraryDefaultsFallback:
    """상태 디렉터리에 없으면 패키지 동봉 기본 프롬프트로 대체한다."""

    def test_상태_디렉터리에_있으면_그쪽을_쓴다(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state_prompts"
        state_dir.mkdir()
        defaults_dir = tmp_path / "pkg_defaults"
        defaults_dir.mkdir()
        write(state_dir / "owner_note.md", "상태 디렉터리 본문")
        write(defaults_dir / "owner_note.md", "패키지 기본 본문")
        library = PromptLibrary(state_dir, defaults_dir=defaults_dir)
        assert library.text("OWNER_NOTE") == "상태 디렉터리 본문"

    def test_상태_디렉터리에_없으면_패키지_기본을_쓴다(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state_prompts"
        state_dir.mkdir()
        defaults_dir = tmp_path / "pkg_defaults"
        defaults_dir.mkdir()
        write(defaults_dir / "owner_note.md", "패키지 기본 본문")
        library = PromptLibrary(state_dir, defaults_dir=defaults_dir)
        assert library.text("OWNER_NOTE") == "패키지 기본 본문"

    def test_둘_다_없으면_MissingPromptError(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state_prompts"
        state_dir.mkdir()
        defaults_dir = tmp_path / "pkg_defaults"
        defaults_dir.mkdir()
        library = PromptLibrary(state_dir, defaults_dir=defaults_dir)
        with pytest.raises(MissingPromptError):
            library.text("OWNER_NOTE")

    def test_defaults_dir을_생략하면_패키지_동봉_자산을_쓴다(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state_prompts"
        state_dir.mkdir()
        library = PromptLibrary(state_dir)
        from slack_cli_agent.core.application import DEFAULT_PROMPT, mode_prompt_names

        required = {DEFAULT_PROMPT, *mode_prompt_names().values()}
        required |= {
            "OWNER_NOTE",
            "NON_OWNER_NOTE",
            "SLACK_FORMAT_RICH",
            "SLACK_FORMAT_PLAIN",
            "POSTMORTEM_NOTE",
            "DEBUG_TRACE_NOTE",
            "FORMAT_REVIEW_NOTE",
            "DIRECTION_NOTE",
            "TRUSTED_NOTE",
            "SENSITIVE_GUARD",
            "FULL_AUTHORITY_NOTE",
            "MECHANISM_NOTE",
            "WATCH_NOTE",
            "WATCH_CHECK_NOTE",
            "CHAT_GUIDE_ACTIVE",
            "CHAT_GUIDE_NORMAL",
            "CHAT_GUIDE_QUIET",
        }
        for name in required:
            assert library.text(name).strip(), f"{name} 기본 프롬프트가 비어 있다"


class Test감시_확인_턴의_안내:
    """확인 턴은 조회만 해야 한다. 일반 감시 안내는 새 감시를 등록하는
    방법이라, 그것을 함께 주면 확인 턴이 새 작업을 띄우도록 유도한다
    (sca-ejy, 코덱스 검토).
    """

    def _조립(self, library: PromptLibrary, knowledge: KnowledgeLoader, *, watch_check: bool) -> str:
        composer = SystemPromptComposer(library, knowledge, base_sections())
        return composer.compose(
            CompositionContext(principal=OWNER, watch_check=watch_check)
        )

    def test_일반_턴은_등록_안내를_받는다(
        self, library: PromptLibrary, knowledge: KnowledgeLoader
    ) -> None:
        본문 = self._조립(library, knowledge, watch_check=False)
        assert "지켜보기 안내" in 본문
        assert "확인만 하는 안내" not in 본문

    def test_확인_턴은_확인_안내만_받는다(
        self, library: PromptLibrary, knowledge: KnowledgeLoader
    ) -> None:
        본문 = self._조립(library, knowledge, watch_check=True)
        assert "확인만 하는 안내" in 본문
        assert "지켜보기 안내" not in 본문
