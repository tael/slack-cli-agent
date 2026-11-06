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

#: MCP tools a read-only turn may keep, listed by exact name. A watch check is
#: a read-only turn, and most watches look at Slack, Jira or a database: with
#: only the built-in tools it could never see the result and answered "still
#: running" until the job aged out (2026-11-06, watch job 22 on guchipachi).
#: Names, not server wildcards: mcp__snowflake-rw__* or a post tool on the same
#: server must not ride in. Metabase stays out -- that server also creates
#: dashboards. Each is kept only when the profile's base tools already carry
#: it, so a read-only turn never gains a server the profile did not have.
READ_ONLY_MCP_TOOLS: tuple[str, ...] = (
    "mcp__slack__slack_list_channels",
    "mcp__slack__slack_channel_history",
    "mcp__slack__slack_from_permalink",
    "mcp__slack__slack_search",
    "mcp__slack__slack_thread_replies",
    "mcp__slack__slack_read_file",
    "mcp__jira__jira_search",
    "mcp__jira__jira_get_issue",
    "mcp__jira__jira_get_comments",
    "mcp__jira__jira_get_transitions",
    "mcp__snowflake__run_snowflake_query",
)


def is_read_only_tool(name: str) -> bool:
    """Whether a tool only reads. The engines use this to keep a turn read-only."""
    return name in READ_ONLY_TOOLS or name in READ_ONLY_MCP_TOOLS


def _granted_by(name: str, base_tools: Sequence[str]) -> bool:
    """Whether the profile's base tools already grant this MCP tool."""
    server = name.rsplit("__", 1)[0]
    return name in base_tools or f"{server}__*" in base_tools


class ToolPolicy:
    def __init__(
        self,
        base_tools: Sequence[str],
        owner_tools: Sequence[str] = (),
        extensions: Sequence[AccessExtension] = (),
        channel_tools: Callable[[Principal], Sequence[str]] | None = None,
        always_skills: Sequence[str] = (),
    ) -> None:
        self._base_tools = tuple(base_tools)
        self._owner_tools = tuple(owner_tools)
        self._extensions = tuple(extensions)
        self._channel_tools = channel_tools
        self._always_skills = tuple(always_skills)

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
        # Standing-clearance skills reach the turn even when the channel has
        # skills off (I83; mametchi's ALWAYS_SKILLS -> Skill(<name>) grants,
        # bot.py:793). Skipped only when the broad Skill tool is already on,
        # which subsumes every named grant.
        if SKILL_TOOL not in tools:
            for skill in self._always_skills:
                grant = f"{SKILL_TOOL}({skill})"
                if grant not in tools:
                    tools.append(grant)
        return tuple(tools)

    def _readonly_tools(self) -> tuple[str, ...]:
        kept = tuple(name for name in self._base_tools if name in READ_ONLY_TOOLS)
        dropped = tuple(name for name in self._base_tools if name not in READ_ONLY_TOOLS)
        if dropped:
            log.warning("읽기 전용 턴에서 뺀 도구 : %s", ", ".join(dropped))
        builtin = kept or READ_ONLY_TOOLS
        mcp = tuple(
            name for name in READ_ONLY_MCP_TOOLS if _granted_by(name, self._base_tools)
        )
        return builtin + mcp

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
