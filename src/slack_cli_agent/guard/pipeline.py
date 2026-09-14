"""Pipeline that applies registered guards in order.

Audit logging isn't this class's responsibility — it only reports what
changed via `PipelineResult.details`. Writing that to the audit log is up
to the caller.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from slack_cli_agent.guard.base import GuardContext, OutputGuard, RerunRequest


@dataclass(frozen=True)
class PipelineResult:
    """Result of one pipeline run.

    A non-None `rerun` means this isn't finished: the caller must invoke the
    engine again with `rerun.rewrite_prompt`, then run the pipeline again on
    the new body. Calling the engine itself is out of scope here.
    """

    body: str
    changed: bool
    applied: tuple[str, ...] = ()
    details: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    rerun: RerunRequest | None = None


class GuardPipeline:
    def __init__(self, guards: Sequence[OutputGuard]) -> None:
        self._guards = list(guards)

    def run(self, body: str, ctx: GuardContext) -> PipelineResult:
        current = body
        applied: list[str] = []
        details: dict[str, Mapping[str, Any]] = {}
        for guard in self._guards:
            result = guard.apply(current, ctx)
            if result.rerun is not None:
                # Stop here; remaining guards must run again from the top
                # once the rewritten body comes back, so order stays correct.
                return PipelineResult(
                    body=current,
                    changed=bool(applied),
                    applied=tuple(applied),
                    details=details,
                    rerun=result.rerun,
                )
            if result.changed:
                applied.append(guard.name)
                details[guard.name] = result.detail
            current = result.body
        return PipelineResult(
            body=current, changed=bool(applied), applied=tuple(applied), details=details
        )
