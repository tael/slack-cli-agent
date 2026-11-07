"""Decides which tools a request gets.

    readonly (postmortem/debug trace)   known read-only tools only
    owner                                base + owner extras + channel extra_tools
    channel operator (operators)         base + the profile's operator_tools (no server
    or delegated controller turn         wildcard), bare Bash/Edit/Write only if operator_full
    an extension applies                 base + whatever the extension adds
    the channel lists the user           base + user_tools, extra_tools, airflow wrapper
    otherwise                            base tools only

Every non-owner turn, and every read-only turn, also gets the profile's
readonly_tools: the lookup bundle (read-only MCP tools, path-named read
wrappers) named one by one. lookups_for tells ToolSelection which of those
names are reads, so the bundle alone never makes a turn able to act.

Read-only by default. Skill is appended last, and only when the request
isn't aside (postmortem/debug/format-check) and the channel has it enabled.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from pathlib import Path

from .policy import OPERATOR_FULL, OPERATOR_TOOLS, AccessExtension
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


#: Browser tools that act on a page rather than read it. The browser profile
#: keeps the owner's logins, so a non-owner never gets these, whatever a
#: profile or channel lists (A9 review: mametchi's non-owner click/type/evaluate
#: was a defect left behind, not a feature to carry over).
BROWSER_ACTION_TOOLS: frozenset[str] = frozenset({
    "browser_click", "browser_type", "browser_evaluate", "browser_fill_form",
    "browser_press_key", "browser_select_option", "browser_drag", "browser_drop",
    "browser_file_upload", "browser_handle_dialog", "browser_hover",
    "browser_run_code_unsafe",
})


#: Server-name fragments that mark a browser server. A server-wide grant on
#: one of these carries every browser action with it.
_BROWSER_SERVER_MARKS: tuple[str, ...] = ("browser", "playwright")


def _granted_server(name: str) -> str | None:
    """The server a server-wide grant opens, or None for a single tool.

    Every spelling the engines read as a whole server counts: mcp__<s>__*,
    a tool pattern such as mcp__<s>__browser_*, mcp__<s> with no tool part,
    mcp__* (every server, returned as "*"), and a bare server name such as
    "gmail" -- engine/claude.py widens that to mcp__<s>__* and codex reads
    profile names that way (A9 review: only the first form was caught).
    Built-in tools are PascalCase and scoped grants carry "(", so neither is
    taken for a server.
    """
    if "(" in name:
        return None
    if name.startswith("mcp__"):
        server, sep, tool = name[len("mcp__"):].partition("__")
        if sep and tool and "*" not in tool:
            return None
        return server.rstrip("*") or "*"
    if name.endswith("*") or name[:1].islower():
        return name.rstrip("*") or "*"
    return None


def _is_server_wildcard(name: str) -> bool:
    return _granted_server(name) is not None


def _is_browser_action(name: str) -> bool:
    server = _granted_server(name)
    if server is not None:
        return server == "*" or any(mark in server for mark in _BROWSER_SERVER_MARKS)
    return name.startswith("mcp__") and name.rsplit("__", 1)[-1] in BROWSER_ACTION_TOOLS


def _non_owner_safe(names: Sequence[str]) -> list[str]:
    kept = [name for name in names if not _is_browser_action(name)]
    if len(kept) != len(names):
        log.warning("비소유자 턴에서 뺀 브라우저 조작 도구와 브라우저 서버 허가 : %s",
                    ", ".join(name for name in names if _is_browser_action(name)))
    return kept


def _granted_by(name: str, base_tools: Sequence[str]) -> bool:
    """Whether the profile's base tools already grant this MCP tool."""
    server = name.rsplit("__", 1)[0]
    return name in base_tools or f"{server}__*" in base_tools


#: Commands a scoped Bash grant must not name: each runs whatever its
#: arguments say or writes files, so Bash(<one of these>:*) is a bare shell
#: under another name. Wrappers outside the home are refused by location;
#: this list catches a copy of one of them placed under the home.
_SHELL_RUNNERS: frozenset[str] = frozenset({
    "sh", "bash", "zsh", "dash", "ksh", "ksh93", "mksh", "csh", "tcsh", "fish",
    "pwsh", "env", "xargs", "sudo", "su", "exec", "eval", "node", "perl", "ruby",
    "php", "osascript", "curl", "wget", "ssh", "scp", "nc", "rm", "mv", "cp",
    "tee", "dd", "chmod", "chown", "awk", "gawk", "find", "sed", "git", "aws",
})

#: Directory names that hold installed programs rather than wrapper scripts.
_PROGRAM_DIRS: frozenset[str] = frozenset({"bin", "sbin", "libexec"})


def _under_home(path: str) -> bool:
    if path.startswith("~/"):
        return True
    home = str(Path.home()).rstrip("/")
    return bool(home) and path.startswith(home + "/")


def _is_read_wrapper(name: str) -> bool:
    """Whether a scoped grant is Bash(<path to a wrapper under the home>...).

    Bash(curl:*) or Bash(python3:*) passed the bare-name filter, and
    engine/claude.py then lifts the bare Bash deny for any scoped Bash grant,
    so a non-owner turn ran that command (A10 review). A path alone was not
    enough either: Bash(/usr/bin/awk:*), Bash(/bin/ksh93:*) and
    Bash(/usr/bin/git:*) passed, and each runs commands or writes files (A10
    re-review). So the check allows rather than blocks: a wrapper is a script
    under the user's home, as mametchi's READ_SHELL_TOOLS are, not in a
    bin/sbin/libexec directory, not named like a shell or general program,
    with no glob, ".." segment or interpreter. Any other scoped grant
    (Edit(...), Write(...)) is not a lookup either.
    """
    tool, _, rest = name.partition("(")
    if tool != "Bash" or not rest.endswith(")"):
        return False
    command = rest[:-1].split(":", 1)[0].split()
    if not command:
        return False
    path = command[0]
    parts = path.split("/")
    base = parts[-1]
    return (
        _under_home(path)
        and "*" not in path
        and ".." not in parts
        and not _PROGRAM_DIRS.intersection(parts[:-1])
        and base not in _SHELL_RUNNERS
        and not base.startswith("python")
    )


def _lookup_bundle(names: Sequence[str]) -> tuple[str, ...]:
    """The profile's readonly_tools as a non-owner may get them (A10).

    mametchi hands every non-owner its lookup MCP tools and read wrappers
    (bot.py:486 ALLOWED_TOOLS, 788 READ_SHELL_TOOLS); sca gave Read, Grep and
    Glob only, so a non-owner asking for a Snowflake figure had no way to look
    it up. Which tools count as lookups is organisation configuration, so the
    profile names them. A server wildcard (any spelling) would open the send
    and delete tools on the same server, a browser action works the owner's
    logins, a bare Bash/Edit/Write writes, and a scoped grant that is not a
    path-named Bash wrapper runs anything: each is dropped whatever the
    profile says.
    """
    def dropped_name(name: str) -> bool:
        if "(" in name:
            return not _is_read_wrapper(name)
        return _is_server_wildcard(name) or _is_browser_action(name) or name in OPERATOR_TOOLS

    dropped = [name for name in names if dropped_name(name)]
    if dropped:
        log.warning("readonly_tools 에서 뺀 도구 : %s", ", ".join(dropped))
    return tuple(dict.fromkeys(name for name in names if name not in dropped))


class ToolPolicy:
    def __init__(
        self,
        base_tools: Sequence[str],
        owner_tools: Sequence[str] = (),
        extensions: Sequence[AccessExtension] = (),
        channel_tools: Callable[[Principal], Sequence[str]] | None = None,
        always_skills: Sequence[str] = (),
        operator_level: Callable[..., str] | None = None,
        operator_tools: Sequence[str] = (),
        readonly_tools: Sequence[str] = (),
    ) -> None:
        self._base_tools = tuple(base_tools)
        self._owner_tools = tuple(owner_tools)
        self._extensions = tuple(extensions)
        self._channel_tools = channel_tools
        self._always_skills = tuple(always_skills)
        self._operator_level = operator_level
        self._operator_grant = tuple(operator_tools)
        self._readonly_grant = _lookup_bundle(readonly_tools)

    def tool_list_for(
        self,
        principal: Principal,
        *,
        prompt: str = "",
        readonly: bool = False,
        aside: bool = False,
        skills_enabled: bool = False,
        asker_is_bot: bool = False,
    ) -> tuple[str, ...]:
        if readonly:
            # Skill is left off as well -- it can run anything, which is the
            # same hole in a different place. Read-only MCP tools come from what
            # this principal would get on a normal turn: guchipachi keeps its
            # MCP servers under owner_tools, so base_tools alone gave a watch
            # check no Slack at all (2026-11-06, watch job 22).
            granted = self.tool_list_for(principal, prompt=prompt, asker_is_bot=asker_is_bot)
            return self._readonly_tools(granted)
        tools: list[str] = list(self._base_tools)
        if principal.trust is TrustLevel.OWNER:
            tools += self._owner_tools
            if self._channel_tools is not None:
                tools += [t for t in self._channel_tools(principal) if t not in tools]
        else:
            tools += [t for t in self._readonly_grant if t not in tools]
            if not aside:
                tools += [t for t in self._operator_tools(principal, asker_is_bot) if t not in tools]
            for ext in self._extensions:
                if ext.applies(principal, prompt):
                    tools += list(ext.extra_tools(principal))
            if self._channel_tools is not None:
                tools += [
                    tool for tool in _non_owner_safe(self._channel_tools(principal))
                    if tool not in tools
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

    def lookups_for(
        self,
        principal: Principal,
        *,
        readonly: bool = False,
        aside: bool = False,
        asker_is_bot: bool = False,
    ) -> tuple[str, ...]:
        """The names in this turn's list that count as reads, not actions.

        ToolSelection.can_act knows only READ_ONLY_TOOLS and
        READ_ONLY_MCP_TOOLS, so the bundle's wrappers and metabase or notion
        lookups made every non-owner turn an action turn, and claude and gemini
        set an MCP server's can_act write gate to "1" for it (A10 review).
        mametchi keeps can_act = is_owner or (operator and not aside) and False
        for everyone else (bot.py build_command); this hands the bundle over
        as reads on exactly those turns. A read-only turn counts it as reads
        for anyone, the owner's watch check included.
        """
        if readonly:
            return self._readonly_grant
        if principal.trust is TrustLevel.OWNER:
            return ()
        if (
            not aside
            and self._operator_level is not None
            and self._operator_level(principal, is_bot=asker_is_bot)
        ):
            return ()
        return self._readonly_grant

    def _operator_tools(self, principal: Principal, is_bot: bool) -> tuple[str, ...]:
        """The profile's operator_tools for a channel operator (mametchi
        OWNER_EXTRA_TOOLS, build_command can_act).

        An explicit list rather than owner_tools: owner_tools names whole
        servers (mcp__gmail__*, mcp__kubernetes__*), and handing those to an
        operator opened the owner's mail and cluster to anyone a channel lists
        (A9 review). A server wildcard is dropped here even when the profile
        writes one, and so is a browser action. The bare write tools stay back
        unless the channel is operator_full -- the same flag engine/claude.py
        reads to lift its settings deny.
        """
        if self._operator_level is None or not self._operator_grant:
            return ()
        level = self._operator_level(principal, is_bot=is_bot)
        if not level:
            return ()
        wildcards = [name for name in self._operator_grant if _is_server_wildcard(name)]
        if wildcards:
            log.warning("operator_tools 의 서버 와일드카드는 붙이지 않는다 : %s", ", ".join(wildcards))
        return tuple(
            name for name in _non_owner_safe(self._operator_grant)
            if not _is_server_wildcard(name)
            and (level == OPERATOR_FULL or name not in OPERATOR_TOOLS)
        )

    def _readonly_tools(self, granted: Sequence[str] = ()) -> tuple[str, ...]:
        kept = tuple(name for name in self._base_tools if name in READ_ONLY_TOOLS)
        dropped = tuple(name for name in self._base_tools if name not in READ_ONLY_TOOLS)
        if dropped:
            log.warning("읽기 전용 턴에서 뺀 도구 : %s", ", ".join(dropped))
        builtin = kept or READ_ONLY_TOOLS
        mcp = tuple(
            name for name in READ_ONLY_MCP_TOOLS if _granted_by(name, tuple(self._base_tools) + tuple(granted))
        )
        # mametchi gives postmortem, debug and no_act turns ALLOWED_TOOLS +
        # READ_SHELL_TOOLS too (bot.py base_tools); a watch check without the
        # wrappers had fewer means than mametchi's (A10 review).
        kept_so_far = builtin + mcp
        return kept_so_far + tuple(name for name in self._readonly_grant if name not in kept_so_far)

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
