# What an engine actually enforces, as opposed to what the caller thinks it
# asked for. claude takes an exact tool allowlist, codex takes a sandbox mode,
# agy takes --dangerously-skip-permissions and enforces nothing. A caller that
# builds allowed_tools and assumes it holds is right on one engine of the three.
#
# The three axes stay separate on purpose. An exact allowlist and a read-only
# sandbox are not stronger and weaker forms of one thing -- the first limits
# which tools exist, the second limits what the process may touch.

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ToolRestriction(StrEnum):
    NONE = "none"
    COARSE_SANDBOX = "coarse_sandbox"
    EXACT_ALLOWLIST = "exact_allowlist"


class ExecutionIsolation(StrEnum):
    NONE = "none"
    WORKSPACE_WRITE = "workspace_write"
    READONLY_SANDBOX = "readonly_sandbox"


class InstructionBoundary(StrEnum):
    #: Untrusted input and system instructions reach the engine in one prompt
    #: string. A separator line is not a boundary -- it is the same layer.
    UNAVAILABLE = "unavailable"
    PROMPT_ONLY = "prompt_only"
    NATIVE = "native"


#: Weakest first. Only compares within one axis; there is no ordering across axes.
_RANK: dict[str, tuple[StrEnum, ...]] = {
    "tool_restriction": tuple(ToolRestriction),
    "execution_isolation": tuple(ExecutionIsolation),
    "instruction_boundary": tuple(InstructionBoundary),
}


@dataclass(frozen=True)
class EngineCapabilities:
    tool_restriction: ToolRestriction = ToolRestriction.NONE
    execution_isolation: ExecutionIsolation = ExecutionIsolation.NONE
    instruction_boundary: InstructionBoundary = InstructionBoundary.UNAVAILABLE


@dataclass(frozen=True)
class ExecutionRequirements:
    """What this request needs. None on an axis means it has no requirement there."""

    tool_restriction: ToolRestriction | None = None
    execution_isolation: ExecutionIsolation | None = None
    instruction_boundary: InstructionBoundary | None = None
    #: Off by default. A default-on relief valve is the same as no enforcement,
    #: since nobody turns it off.
    allow_audited_downgrade: bool = False

    def unmet(self, actual: EngineCapabilities) -> tuple[str, ...]:
        """Axes where `actual` is weaker than required, in declaration order."""
        short: list[str] = []
        for axis, order in _RANK.items():
            required = getattr(self, axis)
            if required is None:
                continue
            if order.index(getattr(actual, axis)) < order.index(required):
                short.append(axis)
        return tuple(short)
