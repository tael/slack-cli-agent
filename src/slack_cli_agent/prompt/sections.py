"""System prompt sections, composed in registration order by SystemPromptComposer."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..auth.principal import Principal, TrustLevel

if TYPE_CHECKING:
    from .knowledge import KnowledgeLoader
    from .library import PromptLibrary

SILENT_MARK = "[침묵]"


@dataclass(frozen=True)
class CompositionContext:
    """Input to a single composition run.

    `library` and `knowledge` are filled in by SystemPromptComposer.compose;
    callers don't need to set them.
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
    # The watch check turn only looks; it must not get the registration
    # guidance, which tells the engine how to start new work (sca-ejy).
    watch_check: bool = False
    watch_run_id: str = ""
    """Result file name for this turn's background work, issued by the code.
    Empty means no name was minted, and the guidance that names it is left
    out — an unfilled slot would reach the engine as a literal (sca-17p)."""
    postmortem: bool = False
    debug_trace: bool = False
    format_review: bool = False
    chat_level: str = "normal"
    silent_mark: str = SILENT_MARK
    library: PromptLibrary | None = None
    knowledge: KnowledgeLoader | None = None
    # (display name, mention) pairs; caller collects these, we just render them (PresentPeopleSection).
    people: tuple[tuple[str, str], ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_owner(self) -> bool:
        return self.principal.trust is TrustLevel.OWNER

    @property
    def full_authority(self) -> bool:
        return self.is_owner and self.principal.is_direct_message

    @property
    def may_disclose_mechanism(self) -> bool:
        return self.full_authority or self.mechanism_open

    @property
    def aside(self) -> bool:
        return self.postmortem or self.debug_trace or self.format_review

    def _library_or_raise(self) -> PromptLibrary:
        if self.library is None:
            raise RuntimeError("CompositionContext 에 library 가 없다. Composer 를 거쳐야 한다")
        return self.library

    def _knowledge_or_raise(self) -> KnowledgeLoader:
        if self.knowledge is None:
            raise RuntimeError("CompositionContext 에 knowledge 가 없다. Composer 를 거쳐야 한다")
        return self.knowledge


class PromptSection(ABC):
    @abstractmethod
    def applies_to(self, ctx: CompositionContext) -> bool: ...

    @abstractmethod
    def render(self, ctx: CompositionContext) -> str: ...


class PersonaSection(PromptSection):
    """Persona, plus internal domain facts when running in a project working directory."""

    def applies_to(self, ctx: CompositionContext) -> bool:
        return True

    def render(self, ctx: CompositionContext) -> str:
        text = ctx._knowledge_or_raise().persona_text(include_domain=ctx.include_domain)
        return f"{text}\n\n" if text else ""


class KnowledgeSection(PromptSection):
    def applies_to(self, ctx: CompositionContext) -> bool:
        return True

    def render(self, ctx: CompositionContext) -> str:
        text = ctx._knowledge_or_raise().knowledge_text(ctx.channel_slug, ctx.prompt)
        return f"{text}\n\n" if text else ""


class RosterSection(PromptSection):
    """Points at the roster file (built by `slack.roster.RosterBuilder`) instead of inlining it.

    Kept out of the prompt body because a roster of hundreds of people is too
    heavy to send on every request. Renders nothing if the file doesn't exist
    yet (e.g. it hasn't been built or lookups keep failing) rather than
    printing a note that implies the file is there.
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
    """Prompt file selected by the channel's mode.

    Leaves the NEVER_DISCLOSE placeholder in place even when disclosure is
    allowed; the Composer strips it later, after AuthoritySection has run,
    to keep the note ordering consistent.
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
    def applies_to(self, ctx: CompositionContext) -> bool:
        return True

    def render(self, ctx: CompositionContext) -> str:
        keep = ("NEVER_DISCLOSE",) if ctx.may_disclose_mechanism else ()
        name = "OWNER_NOTE" if ctx.is_owner else "NON_OWNER_NOTE"
        return ctx._library_or_raise().text(name, keep_slots=keep)


class SlackFormatSection(PromptSection):
    """Rich vs. plain-text formatting rules.

    Unlike other sections, this one isn't appended in place — the Composer
    pulls its output separately and uses it to fill the <<SLACK_FORMAT>>
    placeholder embedded elsewhere in the prompt.
    """

    PLACEHOLDER = "<<SLACK_FORMAT>>"

    def applies_to(self, ctx: CompositionContext) -> bool:
        return True

    def render(self, ctx: CompositionContext) -> str:
        name = "SLACK_FORMAT_RICH" if ctx.is_rich else "SLACK_FORMAT_PLAIN"
        return ctx._library_or_raise().text(name)


class ReviewFormatSection(PromptSection):
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
    """States who's asking — without this, lookups for one person get answered as if they were about another."""

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
    def applies_to(self, ctx: CompositionContext) -> bool:
        return ctx.full_authority or ctx.principal.trust is TrustLevel.TRUSTED

    def render(self, ctx: CompositionContext) -> str:
        return ctx._library_or_raise().text("TRUSTED_NOTE")


class SensitiveGuardSection(PromptSection):
    def applies_to(self, ctx: CompositionContext) -> bool:
        return not ctx.is_owner

    def render(self, ctx: CompositionContext) -> str:
        return ctx._library_or_raise().text("SENSITIVE_GUARD")


class AuthoritySection(PromptSection):
    """Applies when full authority (owner DM) or mechanism disclosure is allowed.

    Either condition also makes the Composer strip the NEVER_DISCLOSE
    placeholder left behind by ChannelModeSection and OwnerNoteSection.
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
    """Skipped during postmortem/debug/format-review — those only look back, nothing to watch for."""

    def applies_to(self, ctx: CompositionContext) -> bool:
        return not ctx.aside

    RUN_ID_SLOT = "<<WATCH_RUN_ID>>"

    def render(self, ctx: CompositionContext) -> str:
        library = ctx._library_or_raise()
        if ctx.watch_check:
            return library.text("WATCH_CHECK_NOTE")
        text = library.text("WATCH_NOTE")
        if ctx.watch_run_id:
            background = library.text("WATCH_BACKGROUND_NOTE", keep_slots=("WATCH_RUN_ID",))
            text += "\n\n" + background.replace(self.RUN_ID_SLOT, ctx.watch_run_id)
        return text


class PresentPeopleSection(PromptSection):
    """Lists who's present in the conversation: whoever has spoken, plus anyone mentioned (a mention notifies them and the thread is open, so treat them as present too).

    Skipped for just two people — that's where someone could get talked about as if absent.
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
