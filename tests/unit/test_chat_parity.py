"""잡담 채널 지침과 입력을 구 마메치(bot.py)와 같게 되돌린다 (2026-11-10).

엔진을 옮긴 뒤 잡담 채널(mode=chat 또는 chat=active) 답이 짧고 업무투로 바뀌었다.
구 봇 지침과 대조해 갈린 지점을 항목별로 시험한다. 봇 멘션 가드는
test_application.py 의 Test잡담채널봇멘션 에 있다.
"""

from __future__ import annotations

from pathlib import Path

from slack_cli_agent.auth.principal import Principal, TrustLevel, TurnTier
from slack_cli_agent.prompt.knowledge import KNOWLEDGE_FIXED, KNOWLEDGE_SELECTED, KnowledgeLoader
from slack_cli_agent.prompt.library import PromptLibrary
from slack_cli_agent.prompt.sections import (
    AskerSection,
    AskSection,
    CompositionContext,
    KnowledgeSection,
    SilenceRuleSection,
    TurnTierSection,
    WorkStyleSection,
)

_PRINCIPAL = Principal(user_id="U1", channel="C1", trust=TrustLevel.GENERAL, is_direct_message=False)


def _ctx(**overrides: object) -> CompositionContext:
    base: dict[str, object] = {
        "principal": _PRINCIPAL,
        "library": PromptLibrary(Path("/does/not/exist")),
    }
    base.update(overrides)
    return CompositionContext(**base)  # type: ignore[arg-type]


def _loader(tmp_path: Path) -> KnowledgeLoader:
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "_common.md").write_text(
        "공통 규칙\n- 지난 정정 <!-- learn:2026-11-01 -->\n", encoding="utf-8",
    )
    (knowledge / "_metabase.md").write_text(
        "<!-- when: 메타베이스 -->\n메타베이스 권한 조회 지식", encoding="utf-8",
    )
    (knowledge / "잡담.md").write_text("채널 지식", encoding="utf-8")
    return KnowledgeLoader(tmp_path / "PERSONA.md", knowledge, always_skills=["metabase"])


class Test학습줄:
    """구 봇 load_persona(bot.py:3596)는 학습 줄을 어느 채널에서나 실었다."""

    def test_잡담_채널은_학습_줄을_싣는다(self, tmp_path: Path) -> None:
        text, _ = _loader(tmp_path).knowledge_selection("잡담", "점심 추천", chat=True)
        assert "지난 정정 <!-- learn:2026-11-01 -->" in text
        assert "학습 기록" not in text

    def test_업무_채널은_학습_줄을_뺀다(self, tmp_path: Path) -> None:
        text, _ = _loader(tmp_path).knowledge_selection("잡담", "점심 추천")
        assert "<!-- learn:" not in text
        assert "학습 기록 1줄은 지침에서 뺐다" in text

    def test_섹션이_채널_모드를_넘긴다(self, tmp_path: Path) -> None:
        loader = _loader(tmp_path)
        for ctx in (
            _ctx(knowledge=loader, channel_slug="잡담", channel_mode="chat"),
            _ctx(knowledge=loader, channel_slug="잡담", chat_level="active"),
        ):
            assert "<!-- learn:" in KnowledgeSection(part="fixed").render(ctx)
        work = _ctx(knowledge=loader, channel_slug="잡담", channel_mode="private")
        assert "<!-- learn:" not in KnowledgeSection(part="fixed").render(work)


class Test늘싣는스킬지식:
    """구 봇은 always 스킬이어도 when 낱말이 안 맞으면 지식을 싣지 않았다."""

    def test_잡담_채널은_낱말이_안_맞으면_싣지_않는다(self, tmp_path: Path) -> None:
        loader = _loader(tmp_path)
        fixed, _ = loader.knowledge_selection("잡담", "점심 추천", part=KNOWLEDGE_FIXED, chat=True)
        picked, _ = loader.knowledge_selection(
            "잡담", "점심 추천", part=KNOWLEDGE_SELECTED, chat=True,
        )
        assert "메타베이스 권한 조회 지식" not in fixed + picked
        assert "metabase :" in picked

    def test_잡담_채널도_낱말이_맞으면_싣는다(self, tmp_path: Path) -> None:
        text, _ = _loader(tmp_path).knowledge_selection("잡담", "메타베이스 권한", chat=True)
        assert "메타베이스 권한 조회 지식" in text

    def test_업무_채널은_늘_싣는다(self, tmp_path: Path) -> None:
        fixed, _ = _loader(tmp_path).knowledge_selection(
            "잡담", "점심 추천", part=KNOWLEDGE_FIXED,
        )
        assert "메타베이스 권한 조회 지식" in fixed


class Test지목안된말기준:
    """구 봇 silence_rule + CHAT_GUIDE (bot.py:2434-2507)와 같은 문구."""

    def _render(self, level: str) -> str:
        return SilenceRuleSection().render(_ctx(unaddressed=True, chat_level=level))

    def test_보통_채널은_가볍게_받는다(self) -> None:
        text = self._render("normal")
        assert "너에 대해 하는 말. 이름을 부르거나 네가 한 일을 두고 하는 말" in text
        assert "한두 마디로 가볍게 받는다. 길게 늘이지 않는다." in text
        assert "정보 요청이나 실행을 기대하는 말에만 응답한다" not in text

    def test_답할_대상을_트리거_발화_하나로_못박는다(self) -> None:
        text = self._render("normal")
        assert "답할 대상은 지금 답할 말 하나다. 그 뒤에 달린 말은 맥락 참고용이다." in text
        assert "그 설명이 그대로 게시된다." in text

    def test_활발한_채널은_농담도_받는다(self) -> None:
        assert "가벼운 농담과 감상. 받아주면 자리가 부드러워진다" in self._render("active")

    def test_조용한_채널은_용건만(self) -> None:
        assert "너를 두고 한 말이라도 용건이 없으면 넘긴다" in self._render("quiet")


class Test업무용블록:
    """구 봇 잡담 지침에 없던 업무 유도 문구는 잡담 채널에서 뺀다."""

    def test_작업방식과_ASK_안내는_잡담_채널에서_빠진다(self) -> None:
        chat = _ctx(channel_mode="chat")
        active = _ctx(chat_level="active", channel_mode="private")
        work = _ctx(channel_mode="private")
        for section in (WorkStyleSection(), AskSection()):
            assert section.applies_to(chat) is False
            assert section.applies_to(active) is False
            assert section.applies_to(work) is True

    def test_권한_고지는_남기고_조회_유도만_뺀다(self) -> None:
        chat = _ctx(channel_mode="chat", turn_tier=TurnTier.READ, turn_reads_only=True)
        text = TurnTierSection().render(chat)
        assert "권한 밖 수단은 '이 턴 권한 밖'이라고 적는다" in text
        assert "조회 도구로 먼저 찾아 답한다" not in text

    def test_업무_채널은_조회_유도를_유지한다(self) -> None:
        work = _ctx(channel_mode="private", turn_tier=TurnTier.READ, turn_reads_only=True)
        assert "조회 도구로 먼저 찾아 답한다" in TurnTierSection().render(work)


class Test멘션뒤이름:
    def test_화자_안내에_한_줄이_들어간다(self) -> None:
        text = AskerSection().render(_ctx(asker_name="신영철", asker_id="UHG"))
        assert "꺾쇠 멘션 뒤에 같은 사람 이름을 다시 쓰지 않는다." in text

    def test_소유자_턴에도_들어간다(self) -> None:
        owner = Principal(user_id="U0", channel="C1", trust=TrustLevel.OWNER, is_direct_message=False)
        text = AskerSection().render(_ctx(principal=owner))
        assert "꺾쇠 멘션 뒤에 같은 사람 이름을 다시 쓰지 않는다." in text
