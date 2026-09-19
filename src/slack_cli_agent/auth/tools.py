"""Decides which tools a request gets.

    readonly (postmortem/debug trace)   known read-only tools only
    owner                                base + owner extras
    an extension applies                 base + whatever the extension adds
    the channel lists the user           base + that user's extra tools
    otherwise                            base tools only

Read-only by default. Skill is appended last, and only when the request
isn't aside (postmortem/debug/format-check) and the channel has it enabled.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from .policy import AccessExtension
from .principal import Principal, TrustLevel

SKILL_TOOL = "Skill"

log = logging.getLogger(__name__)

#: Tools a read-only turn may use. An allowlist rather than a list of write
#: tools: a new write tool, or an MCP tool whose name says nothing about what
#: it does, would pass a denylist silently (sca-gy0). Used as the fallback too:
#: an empty list is not a ban but the absence of one, and the turn then runs
#: with nothing closed at all (sca-6ewc).
READ_ONLY_TOOLS: tuple[str, ...] = ("Read", "Grep", "Glob", "WebFetch", "WebSearch")


class ToolPolicy:
    def __init__(
        self,
        base_tools: Sequence[str],
        owner_tools: Sequence[str] = (),
        extensions: Sequence[AccessExtension] = (),
        channel_tools: Callable[[Principal], Sequence[str]] | None = None,
    ) -> None:
        self._base_tools = tuple(base_tools)
        self._owner_tools = tuple(owner_tools)
        self._extensions = tuple(extensions)
        self._channel_tools = channel_tools

    def tool_list_for(
        self,
        principal: Principal,
        *,
        prompt: str = "",
        readonly: bool = False,
        aside: bool = False,
        skills_enabled: bool = False,
    ) -> tuple[str, ...]:
        if readonly:
            # Skill is left off as well -- it can run anything, which is the
            # same hole in a different place.
            return self._readonly_tools()
        tools: list[str] = list(self._base_tools)
        if principal.trust is TrustLevel.OWNER:
            tools += self._owner_tools
        else:
            for ext in self._extensions:
                if ext.applies(principal, prompt):
                    tools += list(ext.extra_tools(principal))
            if self._channel_tools is not None:
                tools += [
                    tool for tool in self._channel_tools(principal) if tool not in tools
                ]
        if not aside and skills_enabled:
            tools.append(SKILL_TOOL)
        return tuple(tools)

    def _readonly_tools(self) -> tuple[str, ...]:
        kept = tuple(name for name in self._base_tools if name in READ_ONLY_TOOLS)
        dropped = tuple(name for name in self._base_tools if name not in READ_ONLY_TOOLS)
        if dropped:
            log.warning("읽기 전용 턴에서 뺀 도구 : %s", ", ".join(dropped))
        return kept or READ_ONLY_TOOLS

    def tools_for(
        self,
        principal: Principal,
        *,
        prompt: str = "",
        readonly: bool = False,
        aside: bool = False,
        skills_enabled: bool = False,
    ) -> str:
        return ",".join(self.tool_list_for(
            principal, prompt=prompt, readonly=readonly,
            aside=aside, skills_enabled=skills_enabled,
        ))
