"""Strips lines starting with a configured prefix.

Prefixes come from `RuntimeSettings.dropped_line_heads` (default empty), so
this stays generic rather than hardcoding any org-specific text.

Only whole lines starting with a prefix are dropped — a prefix appearing
mid-line as a quote is left alone. A leading dash and the resulting blank
line are cleaned up too.
"""

from __future__ import annotations

from typing import ClassVar

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.guard.base import GuardContext, GuardResult, OutputGuard


class ConfiguredLineDropGuard(OutputGuard):
    name: ClassVar[str] = "dropped_line"

    def __init__(self, settings: RuntimeSettings) -> None:
        self._heads = tuple(settings.dropped_line_heads)

    def apply(self, body: str, ctx: GuardContext) -> GuardResult:
        if not self._heads:
            return GuardResult(body=body, changed=False)
        if not any(head in body for head in self._heads):
            return GuardResult(body=body, changed=False)

        dropped: list[str] = []
        out: list[str] = []
        for line in body.split("\n"):
            bare = line.strip().lstrip("—-–").strip()
            matched = next((head for head in self._heads if bare.startswith(head)), None)
            if matched:
                dropped.append(matched)
                continue
            out.append(line)

        if not dropped:
            return GuardResult(body=body, changed=False)

        new_body = "\n".join(out).rstrip()
        return GuardResult(body=new_body, changed=True, detail={"dropped": dropped})
