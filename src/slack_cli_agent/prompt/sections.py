"""시스템 프롬프트 조각.

원본 build_system_prompt 는 12단계 분기를 한 함수에 담고 있었다. 각 단계를
PromptSection 하나로 만들면 순서가 등록 목록으로 드러나고, 플러그인이 자기
조각을 끼워 넣을 수 있다.

조립 순서는 원본과 같다(01-source-analysis.md 4절 "시스템 프롬프트 조립
순서") —

    1. 페르소나 + 도메인            PersonaSection
    2. 채널 지식                    KnowledgeSection
    3. 채널 모드 프롬프트            ChannelModeSection
    4. OWNER_NOTE / NON_OWNER_NOTE  OwnerNoteSection
    5. SLACK_FORMAT 자리표 치환      SlackFormatSection
    6. POSTMORTEM/DEBUG_TRACE/FORMAT_REVIEW_NOTE   ReviewFormatSection
    7. 화자 안내                    AskerSection
    8. TRUSTED_NOTE                 TrustedSection
    9. SENSITIVE_GUARD              SensitiveGuardSection
    10. FULL_AUTHORITY_NOTE / MECHANISM_NOTE        AuthoritySection
    11. WATCH_NOTE                  WatchSection
    12. silence_rule                SilenceRuleSection
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..auth.principal import Principal, TrustLevel

if TYPE_CHECKING:
    from .knowledge import KnowledgeLoader
    from .library import PromptLibrary

SILENT_MARK = "[침묵]"


@dataclass(frozen=True)
class CompositionContext:
    """조립 한 번의 입력.

    library 와 knowledge 는 SystemPromptComposer.compose 가 채운다. 호출부가
    직접 채울 필요는 없다.
    """

    principal: Principal
    prompt: str = ""
    channel_mode: str = ""
    channel_slug: str = ""
    is_rich: bool = False
    mechanism_open: bool = False
    include_domain: bool = False
    asker_name: str = ""
    asker_id: str = ""
    unaddressed: bool = False
    postmortem: bool = False
    debug_trace: bool = False
    format_review: bool = False
    chat_level: str = "normal"
    silent_mark: str = SILENT_MARK
    library: "PromptLibrary | None" = None
    knowledge: "KnowledgeLoader | None" = None
    # 이 대화에 함께 있는 사람 목록. (표시 이름, 멘션 표기) 쌍이다.
    # 목록을 만드는 것(발화자·멘션 추적)은 이 패키지의 책임이 아니다 —
    # 호출부가 이미 모아 채워 넘긴다. PresentPeopleSection 이 쓴다.
    people: tuple[tuple[str, str], ...] = ()
    extra: dict = field(default_factory=dict)

    @property
    def is_owner(self) -> bool:
        return self.principal.trust is TrustLevel.OWNER

    @property
    def full_authority(self) -> bool:
        """원본의 full_authority. 소유자의 DM 에서만 참이다."""
        return self.is_owner and self.principal.is_direct_message

    @property
    def may_disclose_mechanism(self) -> bool:
        return self.full_authority or self.mechanism_open

    @property
    def aside(self) -> bool:
        """부검·디버그 추적·서식 점검. 평소 대화가 아니다."""
        return self.postmortem or self.debug_trace or self.format_review

    def _library_or_raise(self) -> "PromptLibrary":
        if self.library is None:
            raise RuntimeError("CompositionContext 에 library 가 없다. Composer 를 거쳐야 한다")
        return self.library

    def _knowledge_or_raise(self) -> "KnowledgeLoader":
        if self.knowledge is None:
            raise RuntimeError("CompositionContext 에 knowledge 가 없다. Composer 를 거쳐야 한다")
        return self.knowledge


class PromptSection(ABC):
    """시스템 프롬프트 조각 하나의 추상."""

    @abstractmethod
    def applies_to(self, ctx: CompositionContext) -> bool: ...

    @abstractmethod
    def render(self, ctx: CompositionContext) -> str: ...


class PersonaSection(PromptSection):
    """페르소나와, 도메인 자리(작업 디렉터리)일 때 사내 도메인 사실."""

    def applies_to(self, ctx: CompositionContext) -> bool:
        return True

    def render(self, ctx: CompositionContext) -> str:
        text = ctx._knowledge_or_raise().persona_text(include_domain=ctx.include_domain)
        return f"{text}\n\n" if text else ""


class KnowledgeSection(PromptSection):
    """공통 지식(조건 적재)과 채널별 지식."""

    def applies_to(self, ctx: CompositionContext) -> bool:
        return True

    def render(self, ctx: CompositionContext) -> str:
        text = ctx._knowledge_or_raise().knowledge_text(ctx.channel_slug, ctx.prompt)
        return f"{text}\n\n" if text else ""


class RosterSection(PromptSection):
    """계정 핸들과 사람 이름을 잇는 명부 파일이 있으면 그 경로를 알린다.

    `slack.roster.RosterBuilder` 가 만드는 파일이다. 원본이 이 표를 파일로
    남긴 이유는 매 요청에 싣지 않기 위해서였다(사람 수백 명이면 표 하나가
    가볍지 않다) — 그 판단을 그대로 잇는다. `KnowledgeSection` 이 조건에
    안 맞아 싣지 않은 지식 파일을 이름과 경로만 남기는 것과 같은 방식이다.

    파일이 없으면 아무것도 내지 않는다. 아직 한 번도 갱신되지 않았거나
    조회 실패가 계속돼 만들어진 적이 없는 상태다 — 그 자리를 안내문으로
    채우면 파일이 있는 것처럼 보인다.
    """

    def __init__(self, roster_path: Path) -> None:
        self._roster_path = roster_path

    def applies_to(self, ctx: CompositionContext) -> bool:
        return True

    def render(self, ctx: CompositionContext) -> str:
        if not self._roster_path.exists():
            return ""
        return (
            "\n\n# 계정 핸들과 사람 이름\n\n"
            f"{self._roster_path} 에 계정 핸들과 사람 이름을 잇는 표가 있다.\n"
            "사내 데이터는 사람을 계정 핸들로 남긴다. 계정 핸들이 나오면 이 파일을 "
            "열어 이름을 확인한다. 표에 없는 핸들은 지어내지 않는다.\n"
        )


class ChannelModeSection(PromptSection):
    """채널의 mode 가 고르는 프롬프트 파일.

    구조를 밝혀도 되는 자리(full_authority·mechanism_open)에서는
    NEVER_DISCLOSE 자리표를 지우지 않고 남겨 둔다 — AuthoritySection 뒤에
    Composer 가 지운다. 그래야 안내 순서가 원본과 같다.
    """

    def __init__(self, mode_prompts: dict[str, str], default_prompt: str) -> None:
        self._mode_prompts = dict(mode_prompts)
        self._default_prompt = default_prompt

    def applies_to(self, ctx: CompositionContext) -> bool:
        return True

    def render(self, ctx: CompositionContext) -> str:
        keep = ("NEVER_DISCLOSE",) if ctx.may_disclose_mechanism else ()
        name = self._mode_prompts.get(ctx.channel_mode, self._default_prompt)
        return ctx._library_or_raise().text(name, keep_slots=keep)


class OwnerNoteSection(PromptSection):
    """소유자 요청이면 OWNER_NOTE, 아니면 NON_OWNER_NOTE."""

    def applies_to(self, ctx: CompositionContext) -> bool:
        return True

    def render(self, ctx: CompositionContext) -> str:
        keep = ("NEVER_DISCLOSE",) if ctx.may_disclose_mechanism else ()
        name = "OWNER_NOTE" if ctx.is_owner else "NON_OWNER_NOTE"
        return ctx._library_or_raise().text(name, keep_slots=keep)


class SlackFormatSection(PromptSection):
    """리치·평문 서식 규약.

    본문에 박힌 <<SLACK_FORMAT>> 자리표를 이 조각의 출력으로 채운다. 다른
    조각처럼 그대로 이어붙이지 않는다 — Composer 가 이 조각의 출력을 따로
    떼어 최종 치환 값으로 쓴다.
    """

    PLACEHOLDER = "<<SLACK_FORMAT>>"

    def applies_to(self, ctx: CompositionContext) -> bool:
        return True

    def render(self, ctx: CompositionContext) -> str:
        name = "SLACK_FORMAT_RICH" if ctx.is_rich else "SLACK_FORMAT_PLAIN"
        return ctx._library_or_raise().text(name)


class ReviewFormatSection(PromptSection):
    """부검·디버그 추적·서식 점검 중 하나일 때만 붙는다."""

    def applies_to(self, ctx: CompositionContext) -> bool:
        return ctx.postmortem or ctx.debug_trace or ctx.format_review

    def render(self, ctx: CompositionContext) -> str:
        library = ctx._library_or_raise()
        if ctx.postmortem:
            return library.text("POSTMORTEM_NOTE")
        if ctx.debug_trace:
            return library.text("DEBUG_TRACE_NOTE")
        return library.text("FORMAT_REVIEW_NOTE")


class AskerSection(PromptSection):
    """누가 물었는지 알린다. 화자를 모르면 다른 사람의 조회 결과를 그 사람
    것으로 잘못 답하는 사고가 난다."""

    def applies_to(self, ctx: CompositionContext) -> bool:
        return True

    def render(self, ctx: CompositionContext) -> str:
        library = ctx._library_or_raise()
        if ctx.is_owner:
            return (
                "\n\n지금 말을 건 사람은 소유자 본인이다.\n"
                "이 스레드에 다른 사람도 함께 있을 수 있다. "
                "앞 턴을 말한 사람과 지금 말한 사람이 다를 수 있다.\n"
                "본문 첫 줄 대괄호 안이 시각과 그 말을 한 사람이다. 그것으로만 화자를 판단한다.\n"
                "소유자의 말에 다른 사람을 멘션해 답하지 않는다. "
                "소유자에게 하는 답에는 멘션을 붙이지 않는다."
                + library.text("DIRECTION_NOTE")
            )
        who = ctx.asker_name or "소유자가 아닌 다른 사람"
        note = (
            f"\n\n지금 말을 건 사람은 {who} 이다. 소유자가 아니다.\n"
            "그 사람이 자기 이야기를 하면 그 사람을 가리키는 말이다.\n"
            "봇이 쓰는 계정과 연결은 소유자의 것이다. "
            "조회 결과가 말을 건 사람의 것이라고 단정하지 마라.\n"
            "누구의 상태인지 확실하지 않으면 조회 결과를 그 사람 것으로 제시하지 않는다."
        )
        note += (
            "\n\n이 스레드에 다른 사람도 함께 있을 수 있다. "
            "앞 턴을 말한 사람과 지금 말한 사람이 다를 수 있다.\n"
            "본문 첫 줄 대괄호 안이 시각과 그 말을 한 사람이다. 그것으로만 화자를 판단한다.\n"
            "누구에게 하는 말인지 본문으로 확정되지 않으면 멘션 없이 답한다. "
            "앞 턴에서 멘션했다는 이유로 이어 붙이지 않는다."
        )
        note += library.text("DIRECTION_NOTE")
        if ctx.asker_id:
            note += (
                f"\n\n지금 말을 건 이 사람에게 답할 때는 <@{ctx.asker_id}> 로 멘션한다.\n"
                "이름만 적는 것보다 멘션이 낫다. 자기를 부른 걸 알아채고 대화가 이어진다.\n"
                "한 답변에 한 번이면 충분하다. 문장마다 붙이지 않는다."
            )
        return note


class TrustedSection(PromptSection):
    """신뢰 자리(소유자의 DM, 또는 채널이 신뢰를 준 사용자)에서만 붙는다."""

    def applies_to(self, ctx: CompositionContext) -> bool:
        return ctx.full_authority or ctx.principal.trust is TrustLevel.TRUSTED

    def render(self, ctx: CompositionContext) -> str:
        return ctx._library_or_raise().text("TRUSTED_NOTE")


class SensitiveGuardSection(PromptSection):
    """소유자가 아닌 요청에만 붙는다. 채널과 무관하다."""

    def applies_to(self, ctx: CompositionContext) -> bool:
        return not ctx.is_owner

    def render(self, ctx: CompositionContext) -> str:
        return ctx._library_or_raise().text("SENSITIVE_GUARD")


class AuthoritySection(PromptSection):
    """최고권한(소유자의 DM) 또는 구조 공개(채널 설정)일 때 붙는다.

    둘 중 하나가 참이면 ChannelModeSection·OwnerNoteSection 이 남겨 둔
    NEVER_DISCLOSE 자리표를 Composer 가 지운다.
    """

    FULL_PLACEHOLDER = "<<NEVER_DISCLOSE>>"

    def applies_to(self, ctx: CompositionContext) -> bool:
        return ctx.may_disclose_mechanism

    def render(self, ctx: CompositionContext) -> str:
        library = ctx._library_or_raise()
        if ctx.full_authority:
            return library.text("FULL_AUTHORITY_NOTE")
        return library.text("MECHANISM_NOTE")


class WatchSection(PromptSection):
    """부검·디버그·서식 점검 자리가 아닐 때만 붙는다. 그 자리는 되짚기만
    하고 고치지 않으니 지켜볼 것도 없다."""

    def applies_to(self, ctx: CompositionContext) -> bool:
        return not ctx.aside

    def render(self, ctx: CompositionContext) -> str:
        return ctx._library_or_raise().text("WATCH_NOTE")


class PresentPeopleSection(PromptSection):
    """대화에 함께 있는 사람을 알린다.

    원본 `present_note()` 이식. 말한 사람과 멘션으로 불려 들어온 사람이
    대상이다 — 멘션은 알림이 가고 스레드가 열려 있으므로 그 자리에
    있는 것으로 본다.

    둘뿐이면 굳이 적지 않는다. 셋 이상일 때만 누군가를 없는 사람처럼
    말할 여지가 생긴다.
    """

    def applies_to(self, ctx: CompositionContext) -> bool:
        return len(ctx.people) >= 2

    def render(self, ctx: CompositionContext) -> str:
        lines = ["", "", "이 대화에 함께 있는 사람이다. 말했거나 멘션으로 불려 들어왔다."]
        for name, mention in ctx.people:
            who = f"- {name} {mention}"
            if mention == f"<@{ctx.asker_id}>":
                who += "  (지금 말을 건 사람)"
            lines.append(who)
        lines += [
            "",
            "여기 있는 사람을 없는 사람처럼 말하지 않는다.",
            "멘션으로 불렸으면 알림을 받고 이 스레드를 보고 있다.",
            '"그분께 전달하실 때" 나 "넘기실 때" 처럼 제3자로 두고 말하지 않는다.',
            "그 사람에게 물을 것이 있으면 그 사람을 멘션해 직접 묻는다.",
            "누가 답해야 할 일이면 그 사람을 불러 그렇게 말한다.",
        ]
        return "\n".join(lines)


class SilenceRuleSection(PromptSection):
    """부르지 않은 말에 나설지 정하는 지침. 채널 말수 설정을 따른다."""

    _INTRO = (
        "\n\n이 말은 지목해 부른 것이 아니다. 스레드에서 오간 말이다.\n"
        "답하기 전에 다음 두 가지를 먼저 점검한다.\n"
        "- 다른 사람을 명시적으로 멘션했거나 다른 사람에게 묻는 말인가. "
        "그렇다면 그 사람에게 하는 말이니 나서지 않는다\n"
        "- 정보 요청이나 실행을 기대하는 말인가. 그래야 답할 값어치가 있는 자리다\n"
    )

    def applies_to(self, ctx: CompositionContext) -> bool:
        return ctx.unaddressed

    def render(self, ctx: CompositionContext) -> str:
        library = ctx._library_or_raise()
        guide = library.text(f"CHAT_GUIDE_{ctx.chat_level.upper()}")
        return (
            self._INTRO
            + guide
            + f"\n\n답하지 않기로 했으면 {ctx.silent_mark} 만 출력하고 끝내라. 다른 말을 붙이지 마라."
        )
