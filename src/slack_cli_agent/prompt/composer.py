"""Assembles the system prompt from an ordered list of `PromptSection`s."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from typing import Protocol

from .knowledge import KnowledgeLoader
from .library import PromptLibrary
from .sections import (
    AuthoritySection,
    BudgetAwareSection,
    CompositionContext,
    PromptSection,
    SlackFormatSection,
)

log = logging.getLogger(__name__)

#: Derived in docs/지침-예산.md from the smallest supported context and what
#: must stay free for history, input and the answer -- never from how large
#: the prompt happens to be today (sca-ygd).
DEFAULT_SYSTEM_PROMPT_BUDGET_BYTES = 32768


class PromptComposer(Protocol):
    """What callers need from the composer. SystemPromptComposer satisfies it."""

    def compose(self, ctx: CompositionContext) -> str: ...


class SystemPromptComposer:
    def __init__(
        self,
        library: PromptLibrary,
        knowledge: KnowledgeLoader,
        sections: Sequence[PromptSection],
        budget_bytes: int | None = None,
    ) -> None:
        self._library = library
        self._knowledge = knowledge
        self._sections = tuple(sections)
        # None means no cap. The value comes from docs/지침-예산.md, never from
        # the observed size distribution: a cap set from the numbers it is
        # meant to bound would bound nothing (sca-ygd).
        # 0 or less means off, so an operator can undo the cap without a
        # code change if it ever cuts something it should not have.
        self._budget_bytes = budget_bytes if budget_bytes and budget_bytes > 0 else None

    def compose(self, ctx: CompositionContext) -> str:
        # SlackFormatSection doesn't append its output directly -- instead
        # it fills the <<SLACK_FORMAT>> placeholder wherever other sections
        # left it, so it must resolve after everything else is concatenated.
        # NEVER_DISCLOSE placeholders are cleared last for the same reason,
        # once AuthoritySection has had a chance to fill them in.
        full_ctx = replace(ctx, library=self._library, knowledge=self._knowledge)
        parts: list[str] = []
        format_value = ""
        # Rendered after everything else so the budget-aware sections are told
        # what the sections that can't shrink already took. Their slot in parts
        # is reserved here to keep the composed order unchanged.
        deferred: list[tuple[int, BudgetAwareSection]] = []
        fixed_bytes = 0
        for section in self._sections:
            if not section.applies_to(full_ctx):
                continue
            if isinstance(section, BudgetAwareSection):
                deferred.append((len(parts), section))
                parts.append("")
                continue
            rendered = section.render(full_ctx)
            if isinstance(section, SlackFormatSection):
                format_value = rendered
                continue
            fixed_bytes += len(rendered.encode("utf-8"))
            parts.append(rendered)
        self._fill_deferred(full_ctx, parts, deferred, fixed_bytes)
        text = "".join(parts)
        if format_value:
            text = text.replace(SlackFormatSection.PLACEHOLDER, format_value)
        if full_ctx.may_disclose_mechanism:
            text = text.replace(AuthoritySection.FULL_PLACEHOLDER, "")
        return text

    def _fill_deferred(
        self,
        ctx: CompositionContext,
        parts: list[str],
        deferred: Sequence[tuple[int, BudgetAwareSection]],
        fixed_bytes: int,
    ) -> None:
        if not deferred:
            return
        if self._budget_bytes is None:
            for index, section in deferred:
                parts[index] = section.render(ctx)
            return
        remaining = self._budget_bytes - fixed_bytes
        if remaining <= 0:
            # The sections that can't shrink already filled the budget. Nothing
            # optional goes in, and the operator has to hear about it -- this is
            # a configuration error, not a request that happened to be large.
            log.warning(
                "지침 상한 %d 바이트를 줄일 수 없는 섹션만으로 채웠다 : %d 바이트",
                self._budget_bytes, fixed_bytes,
            )
            remaining = 0
        # Split evenly rather than first-come: one optional section shouldn't be
        # able to starve the next just by being registered earlier.
        share = remaining // len(deferred)
        for index, section in deferred:
            parts[index] = section.render_within(ctx, share)
