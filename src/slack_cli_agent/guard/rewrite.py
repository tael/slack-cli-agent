"""Detects when a rewrite lost content from the previous answer."""

from __future__ import annotations

from typing import ClassVar

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.guard.base import GuardContext, GuardResult, OutputGuard


class RewriteLossGuard(OutputGuard):
    """Flags a rewrite that shrank a long previous answer down drastically —
    prompting alone doesn't reliably prevent this, so it's checked in code.
    Rather than discard the previous content (which no one has seen yet),
    it appends the rewrite after it so both get sent.

    No-op when `ctx.previous_body` is unset (not a rewrite).
    """

    name: ClassVar[str] = "rewrite_loss"

    def __init__(self, settings: RuntimeSettings) -> None:
        self._min_ratio = settings.late_rewrite_min_ratio
        self._min_chars = settings.late_rewrite_min_chars

    def _lost(self, before: str, after: str) -> bool:
        before = (before or "").strip()
        after = (after or "").strip()
        if len(before) < self._min_chars:
            return False
        return len(after) < len(before) * self._min_ratio

    def apply(self, body: str, ctx: GuardContext) -> GuardResult:
        before = ctx.previous_body
        if before is None:
            return GuardResult(body=body, changed=False)
        if not self._lost(before, body):
            return GuardResult(body=body, changed=False)
        merged = f"{before.rstrip()}\n\n{(body or '').strip()}"
        return GuardResult(
            body=merged,
            changed=True,
            detail={
                "lost": True,
                "before_chars": len(before.strip()),
                "after_chars": len((body or "").strip()),
            },
        )
