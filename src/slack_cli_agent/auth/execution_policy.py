"""Decides what an engine must actually guarantee for a request.

The check lives in EngineRunner, but the requirement has to be set where the
request is assembled. Keeping that in one class rather than in pipeline
branches means every caller that builds an EngineRequest -- pipeline, watch,
review, learning -- asks the same question (sca-98k).
"""

from __future__ import annotations

from collections.abc import Sequence

from ..config.channel import TOOL_ENFORCEMENT_STRICT, ChannelConfig
from ..engine.capability import ExecutionRequirements, ToolRestriction

#: No requirement at all. Separate name so the "nothing to enforce" case reads
#: as a decision rather than a forgotten argument.
NO_REQUIREMENTS = ExecutionRequirements()


class ExecutionPolicy:
    """Only the channel file and code-fixed purposes move these levels.

    A Slack message, a trust level or a model's own output never does: the
    tool list is already decided by ToolPolicy from the trust level, and
    re-reading it here would block the owner's ordinary requests on rei.
    """

    def requirements_for(
        self,
        *,
        config: ChannelConfig | None,
        allowed_tools: Sequence[str],
    ) -> ExecutionRequirements:
        # An empty list is not a restriction -- for the Claude CLI it means no
        # tools were named at all. Demanding enforcement of nothing would block
        # engines without giving anyone a boundary.
        if not allowed_tools:
            return NO_REQUIREMENTS
        strict = config is not None and config.tool_enforcement == TOOL_ENFORCEMENT_STRICT
        return ExecutionRequirements(
            tool_restriction=ToolRestriction.EXACT_ALLOWLIST,
            allow_audited_downgrade=not strict,
        )
