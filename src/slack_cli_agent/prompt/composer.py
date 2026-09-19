"""Assembles the system prompt from an ordered list of `PromptSection`s."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from .knowledge import KnowledgeLoader
from .library import PromptLibrary
from .sections import (
    AuthoritySection,
    BudgetAwareSection,
    CompositionContext,
    PromptSection,
    SlackFormatSection,
)


@dataclass(frozen=True)
class PromptBudgetReport:
    """What the budget did to this one composition, in UTF-8 bytes.

    Sizes and counts only, never the text. The audit that carries this is
    readable by people who are not allowed to read the prompts.
    """

    #: None when no cap was in force, which is how "off" and "nothing was
    #: cut" stay apart.
    budget_bytes: int | None
    bytes_before: int
    bytes_after: int
    omitted_document_count: int
    omitted_document_bytes: int

    @property
    def budget_limited(self) -> bool:
        return self.omitted_document_count > 0

    def as_audit_dict(self) -> dict[str, Any]:
        return {
            "system_prompt_budget_bytes": self.budget_bytes,
            "system_prompt_bytes_before_budget": self.bytes_before,
            "system_prompt_bytes_after_budget": self.bytes_after,
            "budget_limited": self.budget_limited,
            "omitted_document_count": self.omitted_document_count,
            "omitted_document_bytes": self.omitted_document_bytes,
        }

log = logging.getLogger(__name__)

#: Derived in docs/지침-예산.md from the smallest supported context and what
#: must stay free for history, input and the answer -- never from how large
#: the prompt happens to be today (sca-ygd).
DEFAULT_SYSTEM_PROMPT_BUDGET_BYTES = 65536


class PromptComposer(Protocol):
    """What callers need from the composer. SystemPromptComposer satisfies it."""

    def compose(self, ctx: CompositionContext) -> str:
        return self.compose_with_report(ctx)[0]

    def compose_with_report(self, ctx: CompositionContext) -> tuple[str, PromptBudgetReport]: ...


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
        return self.compose_with_report(ctx)[0]

    def compose_with_report(self, ctx: CompositionContext) -> tuple[str, PromptBudgetReport]:
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
            parts.append(rendered)
        # Measured after substitution rather than by summing each section: the
        # format body replaces a placeholder that is itself already in parts,
        # so adding both would count that text twice (리뷰 2026-09-19).
        fixed_bytes = len(
            self._substitute("".join(parts), full_ctx, format_value).encode("utf-8")
        )
        omitted_count, omitted_bytes = self._fill_deferred(
            full_ctx, parts, deferred, fixed_bytes,
        )
        text = self._substitute("".join(parts), full_ctx, format_value)
        after = len(text.encode("utf-8"))
        return text, PromptBudgetReport(
            budget_bytes=self._budget_bytes,
            bytes_before=after + omitted_bytes,
            bytes_after=after,
            omitted_document_count=omitted_count,
            omitted_document_bytes=omitted_bytes,
        )

    def _substitute(self, text: str, ctx: CompositionContext, format_value: str) -> str:
        if format_value:
            text = text.replace(SlackFormatSection.PLACEHOLDER, format_value)
        if ctx.may_disclose_mechanism:
            text = text.replace(AuthoritySection.FULL_PLACEHOLDER, "")
        return text

    def _fill_deferred(
        self,
        ctx: CompositionContext,
        parts: list[str],
        deferred: Sequence[tuple[int, BudgetAwareSection]],
        fixed_bytes: int,
    ) -> tuple[int, int]:
        if not deferred:
            return 0, 0
        if self._budget_bytes is None:
            for index, section in deferred:
                parts[index] = section.render(ctx)
            return 0, 0
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
        omitted_count = omitted_bytes = 0
        # Each section is offered everything left rather than a fixed 1/N
        # share: an even split leaves the first section's unused bytes
        # stranded while the next one is cut (리뷰 2026-09-19).
        for index, section in deferred:
            result = section.render_within(ctx, remaining)
            parts[index] = result.text
            remaining = max(0, remaining - len(result.text.encode("utf-8")))
            omitted_count += result.omitted_count
            omitted_bytes += result.omitted_bytes
        return omitted_count, omitted_bytes
