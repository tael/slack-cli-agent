"""Decides which tools a request gets.

    readonly (postmortem/debug trace)   base tools only
    owner                                base + owner extras
    an extension applies                 base + whatever the extension adds
    otherwise                            base tools only

Read-only by default. Skill is appended last, and only when the request
isn't aside (postmortem/debug/format-check) and the channel has it enabled.
"""

from __future__ import annotations

from collections.abc import Sequence

from .policy import AccessExtension
from .principal import Principal, TrustLevel

SKILL_TOOL = "Skill"


class ToolPolicy:
    def __init__(
        self,
        base_tools: Sequence[str],
        owner_tools: Sequence[str] = (),
        extensions: Sequence[AccessExtension] = (),
    ) -> None:
        self._base_tools = tuple(base_tools)
        self._owner_tools = tuple(owner_tools)
        self._extensions = tuple(extensions)

    def tools_for(
        self,
        principal: Principal,
        *,
        prompt: str = "",
        readonly: bool = False,
        aside: bool = False,
        skills_enabled: bool = False,
    ) -> str:
        tools: list[str] = list(self._base_tools)
        if readonly:
            pass
        elif principal.trust is TrustLevel.OWNER:
            tools += self._owner_tools
        else:
            for ext in self._extensions:
                if ext.applies(principal, prompt):
                    tools += list(ext.extra_tools(principal))
        if not aside and skills_enabled:
            tools.append(SKILL_TOOL)
        return ",".join(tools)
