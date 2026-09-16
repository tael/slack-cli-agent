"""Assembles the system prompt from an ordered list of `PromptSection`s."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Protocol

from .knowledge import KnowledgeLoader
from .library import PromptLibrary
from .sections import AuthoritySection, CompositionContext, PromptSection, SlackFormatSection


class PromptComposer(Protocol):
    """What callers need from the composer. SystemPromptComposer satisfies it."""

    def compose(self, ctx: CompositionContext) -> str: ...


class SystemPromptComposer:
    def __init__(
        self,
        library: PromptLibrary,
        knowledge: KnowledgeLoader,
        sections: Sequence[PromptSection],
    ) -> None:
        self._library = library
        self._knowledge = knowledge
        self._sections = tuple(sections)

    def compose(self, ctx: CompositionContext) -> str:
        # SlackFormatSection doesn't append its output directly -- instead
        # it fills the <<SLACK_FORMAT>> placeholder wherever other sections
        # left it, so it must resolve after everything else is concatenated.
        # NEVER_DISCLOSE placeholders are cleared last for the same reason,
        # once AuthoritySection has had a chance to fill them in.
        full_ctx = replace(ctx, library=self._library, knowledge=self._knowledge)
        parts: list[str] = []
        format_value = ""
        for section in self._sections:
            if not section.applies_to(full_ctx):
                continue
            rendered = section.render(full_ctx)
            if isinstance(section, SlackFormatSection):
                format_value = rendered
                continue
            parts.append(rendered)
        text = "".join(parts)
        if format_value:
            text = text.replace(SlackFormatSection.PLACEHOLDER, format_value)
        if full_ctx.may_disclose_mechanism:
            text = text.replace(AuthoritySection.FULL_PLACEHOLDER, "")
        return text
