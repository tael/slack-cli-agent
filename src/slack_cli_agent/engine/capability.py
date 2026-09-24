# What an engine actually enforces, as opposed to what the caller thinks it
# asked for. claude takes an exact tool allowlist, codex takes a sandbox mode,
# agy takes --dangerously-skip-permissions and enforces nothing. A caller that
# builds allowed_tools and assumes it holds is right on one engine of the three.
#
# The three axes stay separate on purpose. An exact allowlist and a read-only
# sandbox are not stronger and weaker forms of one thing -- the first limits
# which tools exist, the second limits what the process may touch.

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class ToolRestriction(StrEnum):
    NONE = "none"
    COARSE_SANDBOX = "coarse_sandbox"
    #: Exact for the built-in set, per server for MCP. claude's --tools closes
    #: built-ins by name, but MCP tools only close with a per-server wildcard:
    #: a server holding one allowed tool stays open whole and its other tools
    #: are left to the approval rules (2026-09-20 measurement, sca-6ewc). Below
    #: EXACT_ALLOWLIST because the allowlist does not hold at tool granularity
    #: there (sca-vo05).
    SERVER_SCOPED_ALLOWLIST = "server_scoped_allowlist"
    EXACT_ALLOWLIST = "exact_allowlist"
    #: No tools at all -- the allowlist taken to zero, so it belongs at the
    #: top of this axis rather than on one of its own. Only claude holds it,
    #: with --disallowedTools=* (2026-09-19 measurement: the debug log stops
    #: loading tools and tool_use never appears). Denying tools by name is
    #: not this -- the model reaches the same file through another tool.
    ALL_FORBIDDEN = "all_forbidden"


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


#: Axis names, as the dataclass fields spell them. Here rather than in the
#: runner so a caller can name an axis without importing the run path.
TOOL_AXIS = "tool_restriction"
ISOLATION_AXIS = "execution_isolation"
BOUNDARY_AXIS = "instruction_boundary"

@dataclass(frozen=True)
class ExecutionRequirements:
    """What this request needs. None on an axis means it has no requirement there."""

    tool_restriction: ToolRestriction | None = None
    execution_isolation: ExecutionIsolation | None = None
    instruction_boundary: InstructionBoundary | None = None
    #: Axes the caller will give up if the engine can't hold them, provided the
    #: downgrade is recorded. Per axis rather than one flag: a tool-only setting
    #: must not also lower isolation, which is a separate guarantee (sca-igu).
    #: Empty by default -- a default-on relief valve is the same as no
    #: enforcement, since nobody turns it off.
    downgradable_axes: frozenset[str] = frozenset()
    #: Which policy set this, for the audit record. Empty means nobody did --
    #: that reads differently from a policy that decided to require nothing.
    policy: str = ""

    def downgraded(self, unmet: Sequence[str]) -> tuple[str, ...]:
        """Axes actually given up. Empty when any unmet axis is outside
        `downgradable_axes`: one axis the caller insisted on blocks the run,
        and then nothing is relieved."""
        if not unmet or any(axis not in self.downgradable_axes for axis in unmet):
            return ()
        return tuple(unmet)

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
