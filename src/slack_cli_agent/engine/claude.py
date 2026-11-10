"""Claude Code CLI adapter.

Paths go through --add-dir. We mint the session ID and pass it via
--session-id; continuing turns pass the same value via --resume. The
system prompt goes every turn via --append-system-prompt, but claude 2.1.319
does not apply it on --resume: the resumed request carries the system text of
the turn that opened the session (measured 2026-11-07, live session 4efce5d9
and a CLI probe that swapped the value between two turns). So a turn whose
instructions differ in kind -- another speaker tier -- must not resume;
session/manager.py opens a new session for it.

MCP servers (sca-kos.2): claude --help documents "--mcp-config <configs...>
Load MCP servers from JSON files or strings (space-separated)", so the
converted Profile.mcp_servers go straight in as one inline JSON string —
no file write needed. --strict-mcp-config keeps this bot from picking up
any other bot's project/user-level MCP config. The remote-server shape
(type/url/headers) and stdio shape (command/args/env/cwd) both come from
code.claude.com/docs/en/mcp; there's no disabledTools-equivalent key
documented for claude's .mcp.json, so McpServerSpec.disabled_tools is
silently dropped for this engine (agy has serverUrl/disabledTools instead,
see gemini.py).

Skills (sca-kos.3): code.claude.com/docs/en/skills states "--add-dir 로
추가한 디렉터리의 .claude/skills/ 에서 스킬을 읽는다" — a bot's own
StatePaths.skills, passed the same way as any other --add-dir path, is
enough to keep one bot's skills out of another's session.

That discovery path is also unwritable: in dontAsk mode Write and Bash are
refused on any path with a `.claude` component, --add-dir or not (실측
2026-09-19, so a bot could never install its own skill). prepare() therefore
points it at StatePaths.skill_files with a symlink; discovery follows the
link and writes go to the plain path.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import itertools
import json
import logging
import shlex
import sys
import uuid
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar

from ..auth.policy import OPERATOR_TOOLS
from ..auth.principal import TrustLevel
from ..config.profile import EngineSpec, McpServerSpec
from ..guard import finish_hook
from ..guard.posttool import parallel_nudge
from ..guard.pretool import (
    install_guard,
    plan_gate,
    repeat_guard,
    restart_guard,
    scan_guard,
    serial_read_guard,
    sleep_poll_guard,
    turn_budget,
)
from ..observability.progress_hook import HOOK_SCRIPT
from .base import (
    ElapsedSource,
    Engine,
    EngineRequest,
    EngineResponse,
    FailureDetail,
    ModelUsage,
    Usage,
    UsageLimit,
    acts_for_owner,
)
from .capability import (
    EngineCapabilities,
    ExecutionIsolation,
    InstructionBoundary,
    ToolRestriction,
)
from .claude_settings import load_merged_settings, merge_settings
from .footprint import PayloadFootprint
from .tool_selection import ToolAccess

# Same hint list as the original bot.py's usage_limit_message().
_USAGE_LIMIT_HINTS = (
    "weekly limit", "usage limit", "rate limit",
    "hit your limit", "limit · resets", "limit reached",
    # claude 2.1.318 문구. "You've hit your ${종류} limit", 팀 플랜의 사용량
    # 소진 문구가 위 목록에 없어 한도가 일반 실패로 떨어졌다 (2026-11-06).
    "you've hit your", "you've reached your", "session limit",
    "out of extra usage", "out of usage credits", "org is out of usage",
    "monthly spend limit", "spend limit reached",
)

# Claude's own vocabulary — its native keys already match the common ones,
# unlike codex/gemini which need real translation. Kept as a key_map anyway
# (rather than a from_mapping() special case) so a partial usage dict is
# read the same way every engine reads one: a missing key is "can't tell",
# not "measured zero".
log = logging.getLogger(__name__)

_USAGE_KEY_MAP: Mapping[str, str] = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_creation_tokens": "cache_creation_input_tokens",
    "cache_read_tokens": "cache_read_input_tokens",
}


#: What every MCP tool name starts with: mcp__<server>__<tool>.
MCP_PREFIX = "mcp__"


def _server_wide_names(names: tuple[str, ...], servers: Iterable[str]) -> tuple[str, ...]:
    """A bare server name in the allowlist means that whole server.

    Profiles list MCP servers by name (owner_tools: ["snowflake", "csv"]), the
    form the codex engine takes. Read as-is here, those names matched no
    mcp__<server>__ prefix, so _mcp_denials closed every server with mcp__*
    and the same names went into --tools as built-ins that do not exist.
    2026-11-06 deagent: 21 test turns on this engine called no MCP tool at all.
    """
    server_set = set(servers)
    return tuple(f"{MCP_PREFIX}{name}__*" if name in server_set else name for name in names)


#: The built-in tool that loads deferred MCP tool schemas on demand.
TOOL_SEARCH = "ToolSearch"

#: The subagent tool under its current and former name. The init event lists
#: it as Task when --tools names Agent (claude 2.1.321).
AGENT_TOOLS = frozenset({"Agent", "Task"})


def _closed_tool_names(args: Sequence[str]) -> dict[str, tuple[str, ...]]:
    """The tool lists a turn's arguments carry, for the audit row (names only).

    Reads _tool_args' output rather than rebuilding the lists, so the record
    cannot disagree with the command that ran. A flag the turn did not pass is
    left out: "not closed" and "closed to nothing" stay apart.
    """
    keys = {
        "--tools": "tools_builtin",
        "--allowedTools": "tools_allowed",
        "--disallowedTools": "tools_disallowed",
    }
    found: dict[str, tuple[str, ...]] = {}
    for flag, value in itertools.pairwise(args):
        key = keys.get(flag)
        if key is not None:
            found[key] = tuple(name for name in value.split(",") if name)
    return found


def _builtin_names(names: tuple[str, ...]) -> list[str]:
    """The built-in tools among what the caller allowed.

    --tools takes names from the built-in set. MCP tools are not in it, so
    they ride on --allowedTools alone. An empty result is still passed: dropping the argument opens every
    built-in tool, which is the opposite of an allowlist naming none.

    A scoped grant goes in as its bare tool. --tools "Read,Bash(echo:*)" left
    Bash off the model's list -- the turn answered that it had no Bash tool --
    while --tools "Read,Bash" with the same --allowedTools ran echo and refused
    ls in dontAsk (claude 2.1.319, measured 2026-11-07). The scope is held by
    --allowedTools, which keeps the scoped form and never the bare name. The
    owner's format review lost its rewrite command this way: "No such tool
    available: Bash" (#test-agent 1794051030.555249, B6).
    """
    builtins = (
        name.split("(", 1)[0] if "(" in name and name.endswith(")") else name
        for name in names if not name.startswith(MCP_PREFIX)
    )
    return list(dict.fromkeys(builtins))


def _mcp_denials(names: tuple[str, ...], servers: Iterable[str]) -> list[str]:
    """Patterns closing the MCP servers the allowlist does not name.

    --tools covers the built-in set only, so a profile's MCP tools stay on the
    model's list whatever the allowlist says. dontAsk refuses them at approval
    time, but that is a permission rule -- a name in --allowedTools or in
    settings' allow opens it.

    The server is as narrow as this gets. Measured 2026-09-20:
    --disallowedTools "mcp__playwright__*" removed that server's tools and left
    mcp__playwright-daangn__ alone, so the pattern cuts at the server and not at
    the shared prefix. The other direction does not work -- closing a server and
    naming one of its tools in --allowedTools did not bring that tool back, the
    wildcard wins. So a server holding an allowed tool stays open whole, and its
    other tools are left to the approval list (sca-6ewc).

    A server counts as allowed only when a name actually sits under it. Reading
    the server out of the name instead splits mcp__foo__bar__search at the first
    __ and takes mcp__jira for the whole jira server, both of which open servers
    the allowlist never named (codex review). `servers` is what --mcp-config
    carries, so a disabled server is not among them and nothing opens under it.
    """
    allowed = _open_mcp_servers(names, servers)
    if not allowed:
        return [f"{MCP_PREFIX}*"]
    return [f"{MCP_PREFIX}{server}__*" for server in sorted(servers) if server not in allowed]


def _lift_scoped_denials(settings: dict[str, Any], names: Iterable[str]) -> dict[str, Any]:
    """Drop a bare deny entry the allowlist narrows with a scoped grant.

    general's overlay denies the bare "Bash", and deny wins over allow, so an
    airflow_admins `Bash(<airflow_ctl>:*)` or a channel's scoped Bash grant was
    refused on this engine (A9 review). mametchi removed the same deny on
    2026-10-02 for that reason (bot.py build_command). The bare name stays out
    of the allowlist, and dontAsk refuses any call no allow entry matches, so
    only the scoped form opens. Only Bash is lifted: that is the tool the
    scoped grants exist for, and the other deny entries are left as they are.
    """
    scoped = {"Bash"} & {
        name.split("(", 1)[0] for name in names if "(" in name and name.endswith(")")
    }
    permissions = settings.get("permissions")
    if not scoped or not isinstance(permissions, Mapping):
        return settings
    deny = permissions.get("deny")
    if not isinstance(deny, list) or not scoped & set(deny):
        return settings
    kept = [entry for entry in deny if entry not in scoped]
    return {**settings, "permissions": {**permissions, "deny": kept}}


def _deny_ledger_writes(settings: dict[str, Any], *ledgers: Path | None) -> dict[str, Any]:
    """Deny the file tools on the delegation ledgers for a non-owner turn.

    Both ledgers count: delegation_ledger (read here) and delegation_record
    (written here, read by the peer bot, A8). A row forged in the latter makes
    the peer bot treat a message as delegated by this bot.

    The ledger is a JSON file owned by the same OS user, so the by check in
    auth/delegation.py only stops an honest writer. A delegated turn in an
    operator_full channel holds bare Write/Edit and could add a row naming
    the next controller message, delegating to itself (A7 review). Bash is
    not covered: an Edit/Write deny does not reach a shell redirect, and the
    bare Bash an operator_full channel hands out is an unrestricted shell.
    """
    extra: list[str] = []
    for ledger in ledgers:
        if ledger is None:
            continue
        path = ledger.expanduser().resolve()
        extra += [f"Edit(/{path})", f"Write(/{path})"]
    if not extra:
        return settings
    permissions = settings.get("permissions")
    permissions = dict(permissions) if isinstance(permissions, Mapping) else {}
    deny = list(permissions.get("deny") or [])
    permissions["deny"] = deny + [entry for entry in extra if entry not in deny]
    return {**settings, "permissions": permissions}


def _named_mcp_servers(names: Iterable[str], servers: Iterable[str]) -> set[str]:
    """허용목록 이름이 가리키는 서버. 서버 자리에 * 가 든 이름(mcp__*__*,
    mcp__*)은 패턴으로 읽어 맞는 서버를 모두 넣는다. 와일드카드 허용을 쓰던
    프로필이 서버를 잃지 않게 하려는 것이다 (S2 1단계)."""
    server_list = list(servers)
    named: set[str] = set()
    for name in names:
        if name in server_list:
            named.add(name)
            continue
        if not name.startswith(MCP_PREFIX):
            continue
        rest = name[len(MCP_PREFIX):]
        server_part = rest.split("__", 1)[0]
        if "*" in server_part or "?" in server_part:
            named.update(s for s in server_list if fnmatch.fnmatchcase(s, server_part))
        else:
            named.update(s for s in server_list if rest.startswith(f"{s}__"))
    return named


def _attached_mcp_servers(
    request: EngineRequest, servers: Iterable[str]
) -> list[str]:
    """이 턴에 --mcp-config 로 붙일 서버 이름.

    1단계: 허용목록(ALLOWLIST) 턴은 허용 이름이 하나도 없는 서버를 붙이지
    않는다. 붙여 봐야 --disallowedTools 로 닫히는 서버라 동작은 같고, 도구
    정의만 컨텍스트에서 빠진다. 도구 금지(FORBIDDEN) 턴은 어떤 서버도 못 쓰니
    아무것도 붙이지 않는다. 제한 없는(UNRESTRICTED) 턴은 그대로 다 붙인다.
    2단계: request.mcp_selection 이 있으면 그 안의 서버만 남긴다.
    """
    attached = list(servers)
    if request.tools.access is ToolAccess.FORBIDDEN:
        return []
    if request.tools.access is ToolAccess.ALLOWLIST:
        named = _named_mcp_servers(request.tools.names, attached)
        attached = [s for s in attached if s in named]
    if request.mcp_selection is not None:
        attached = [s for s in attached if s in request.mcp_selection]
    return attached


def _open_mcp_servers(names: tuple[str, ...], servers: Iterable[str]) -> set[str]:
    """The servers left open, tools the allowlist never named included."""
    return {
        server for server in servers
        if any(name.startswith(f"{MCP_PREFIX}{server}__") for name in names)
    }


#: Empty attribution for commits and PR bodies. --setting-sources "" drops the
#: operator's ~/.claude/settings.json, so its own "attribution" block never
#: reached the bot: claude then told the model to end PR bodies with
#: "Generated with Claude Code" and commits with a Co-Authored-By trailer.
#: That went out in meshkorea/vroong-meshdata-airflow #729 and #730
#: (2026-11-05). Merged last so no profile file can turn it back on.
NO_ATTRIBUTION_SETTINGS: dict[str, Any] = {"attribution": {"commit": "", "pr": ""}}


def progress_hook_settings(log_path: Path) -> dict[str, Any]:
    """A settings fragment registering the tool-start hook for one request.

    Goes in through --settings as JSON rather than into the bot's engine
    home: the log path differs per request, and a file in the home would
    be read by every concurrently running request, including ones in a
    channel with progress off.

    The interpreter is this process's own (sys.executable) because the
    engine's environment is a minimal allowlist (engine/environment.py) —
    a bare "python3" would depend on whatever that PATH happens to hold.
    """
    command = " ".join(
        shlex.quote(part) for part in (sys.executable, str(HOOK_SCRIPT), str(log_path))
    )
    return {
        "hooks": {
            "PreToolUse": [
                {"matcher": "*", "hooks": [{"type": "command", "command": command}]}
            ]
        }
    }


def _hook_command(script: Path) -> str:
    """``<sys.executable> <script>`` as one shell-quoted command string, the
    same shape progress_hook_settings() builds for its own hook."""
    return " ".join(shlex.quote(part) for part in (sys.executable, str(script)))


#: Engine-default PreToolUse Bash guards, in the order they should run:
#: turn budget first (cheapest, no filesystem walk), then the self-restart
#: deny (a cheap regex, but it should block a deadlocking restart before
#: any disk work), then the two guards that read session state off disk.
#: Unlike progress_hook_settings() this
#: fragment does not vary per request -- it is the same for every turn, so
#: it is built once at import time rather than inside _settings_value().
_GUARD_HOOK_SETTINGS: dict[str, Any] = {
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {"type": "command", "command": _hook_command(turn_budget.HOOK_SCRIPT)},
                    {"type": "command", "command": _hook_command(restart_guard.HOOK_SCRIPT)},
                    {"type": "command", "command": _hook_command(install_guard.HOOK_SCRIPT)},
                    {"type": "command", "command": _hook_command(scan_guard.HOOK_SCRIPT)},
                    {"type": "command", "command": _hook_command(sleep_poll_guard.HOOK_SCRIPT)},
                ],
            },
            # Every tool: denies a read-only call after a run of responses that
            # each made one read-only call (guard/pretool/serial_read_guard).
            # The posttool nudge below only reminds and was ignored in practice.
            {
                "matcher": "*",
                "hooks": [
                    {"type": "command", "command": _hook_command(serial_read_guard.HOOK_SCRIPT)},
                    # Denies a call from the third tool-calling response when no
                    # `[계획]` block came first; only on turns the pipeline set
                    # PLAN_GATE for (guard/pretool/plan_gate, W4).
                    {"type": "command", "command": _hook_command(plan_gate.HOOK_SCRIPT)},
                    # Denies a call after the previous responses each made an
                    # essentially-same call (guard/pretool/repeat_guard).
                    {"type": "command", "command": _hook_command(repeat_guard.HOOK_SCRIPT)},
                ],
            },
        ],
        # Never blocks; only reminds the model to batch independent lookups
        # after a run of single read-only calls (guard/posttool/parallel_nudge).
        "PostToolUse": [
            {
                "matcher": "*",
                "hooks": [
                    {"type": "command", "command": _hook_command(parallel_nudge.HOOK_SCRIPT)},
                ],
            },
        ],
        "Stop": [
            {
                "matcher": "*",
                "hooks": [{"type": "command", "command": _hook_command(finish_hook.HOOK_SCRIPT)}],
            },
        ],
    }
}


def guard_hook_settings() -> dict[str, Any]:
    """The settings fragment registering the Bash PreToolUse guards
    (turn_budget, restart_guard, install_guard, scan_guard, sleep_poll_guard -- guard/pretool/),
    the all-tools serial-read, plan and repeat guards (guard/pretool/serial_read_guard.py,
    guard/pretool/plan_gate.py, guard/pretool/repeat_guard.py),
    the parallel-nudge PostToolUse hook (guard/posttool/parallel_nudge.py)
    and the finish-review Stop hook (guard/finish_hook.py) as engine built-ins.

    Unlike progress_hook_settings(), nothing here is per-request: every
    turn on this engine gets the same four Bash guards and the same Stop
    hook with zero profile configuration, which is the point of landing
    them as engine defaults rather than mametchi's own per-bot hook setup
    (ownership.md: B4). Returns the same (new) dict each call so a caller
    merging it in can't accidentally mutate the shared fragment.
    """
    fragment: dict[str, Any] = json.loads(json.dumps(_GUARD_HOOK_SETTINGS))
    return fragment


def _claude_mcp_servers(
    mcp_servers: Mapping[str, McpServerSpec],
    *,
    can_act: bool = False,
    is_owner: bool = False,
    turn_context: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    servers: dict[str, Any] = {}
    for name, server in mcp_servers.items():
        if server.disabled:
            continue
        entry: dict[str, Any]
        if server.is_remote:
            entry = {"type": "http", "url": server.url}
            if server.headers:
                entry["headers"] = server.resolved_headers()
        else:
            entry = {"command": server.command}
            if server.args:
                entry["args"] = list(server.args)
            # turn_env rides on the static env: a read-only turn drops the gate
            # var entirely, which is how the server reads "no write this turn",
            # and the channel var drops when no channel token is in context.
            env = server.resolved_env()
            env.update(
                server.turn_env_for(
                    can_act=can_act, is_owner=is_owner, turn_context=turn_context
                )
            )
            if env:
                entry["env"] = env
            if server.cwd:
                entry["cwd"] = str(server.cwd)
        servers[name] = entry
    return servers


class ClaudeEngine(Engine):
    name = "claude"

    #: ccusage reads Claude Code's session records, which is exactly this engine.
    ccusage_reports_consumption = True

    #: --disallowedTools=* empties the tool set, so the ban needs no wording.
    enforces_tool_ban = True

    # --allowedTools overrides settings.json's allow rules, and the system prompt
    # is its own flag. There is no filesystem sandbox: read-only is held by the
    # tool list, which is the tool axis rather than this one.
    capabilities = EngineCapabilities(
        tool_restriction=ToolRestriction.EXACT_ALLOWLIST,
        execution_isolation=ExecutionIsolation.NONE,
        instruction_boundary=InstructionBoundary.NATIVE,
    )

    def _tool_args(self, request: EngineRequest, mcp_servers: Mapping[str, Any]) -> list[str]:
        """The arguments that close this turn's tool set, if anything does.

        capabilities_for reads the same list rather than the request: a
        declaration taken from what the caller asked for is always equal to
        what the policy requires, so the audit could never record a turn where
        the tools did not actually close (sca-wu50).
        """
        args: list[str] = []
        if request.tools.access is ToolAccess.FORBIDDEN:
            # The wildcard empties the tool set itself: the debug log stops
            # loading tools and tool_use never appears (2026-09-19 measurement,
            # sca-0a7). No --allowedTools beside it, so the command matches
            # what was measured -- and the wildcard wins over it anyway.
            args += ["--disallowedTools", "*"]
        elif request.tools.access is ToolAccess.ALLOWLIST:
            # --allowedTools only adds auto-approval; it does not close the
            # tools left out of it. Measured 2026-09-20 (claude 2.1.263):
            # --allowedTools "Read" still ran Bash, with and without
            # --setting-sources project and an empty permissions file. The same
            # path with deny in it removed the tool, so the file was read.
            # --tools is what closes them: it names the built-in set itself,
            # and a Write call under --tools "Read,Grep,Glob" came back as
            # "No such tool available" (sca-6ewc).
            names = _server_wide_names(request.tools.names, mcp_servers)
            builtins = _builtin_names(names)
            if _open_mcp_servers(names, mcp_servers) and TOOL_SEARCH not in builtins:
                # Without ToolSearch on the list claude loads every MCP tool
                # schema into each call up front; with it the schemas are
                # deferred and fetched on demand. Measured 2026-11-08 (claude
                # 2.1.319, one kubernetes server, 21 tools): 12,219 input
                # tokens without it, 4,943 with it. guchipachi's 16 servers
                # put about 158k of a 268k first call into tool schemas (S2).
                builtins.append(TOOL_SEARCH)
                names = (*names, TOOL_SEARCH)
            args += ["--allowedTools", ",".join(names)]
            args += ["--tools", ",".join(builtins)]
            denied = _mcp_denials(names, mcp_servers)
            if denied:
                args += ["--disallowedTools", ",".join(denied)]
        return args

    def _agent_args(self, request: EngineRequest) -> list[str]:
        """--agents with the profile's subagent definitions, on closed turns
        whose tool set has the Agent tool.

        An unrestricted turn gets nothing: the profile's definitions are
        written against the owner's closed set, and a turn with every tool
        open is not the one they were measured for. A turn without Agent gets
        nothing either -- the CLI lists the agents there but the model has no
        tool to call them with (claude 2.1.321, w3_inheritance.md case E).
        """
        agents = self.profile.agents
        if not agents or request.tools.access is not ToolAccess.ALLOWLIST:
            return []
        if not AGENT_TOOLS.intersection(_builtin_names(request.tools.names)):
            return []
        payload = {name: definition.cli_entry() for name, definition in agents.items()}
        return ["--agents", json.dumps(payload, ensure_ascii=False)]

    @classmethod
    def configured_capabilities(
        cls, spec: EngineSpec, mcp_servers: Mapping[str, McpServerSpec]
    ) -> EngineCapabilities:
        """The ceiling a claude profile can reach, whatever a request asks.

        An attached server is what capabilities_for() drops to server
        granularity for, and which servers a turn leaves open depends on that
        turn's allowlist. Boot has no request, so the answer here is the
        weakest a request could make it: one attached server is enough. The
        other direction -- reporting exact_allowlist -- would tell the operator
        the allowlist holds per tool when no turn on that profile can promise
        it (sca-qqtl).

        Reads `disabled` directly rather than through _claude_mcp_servers:
        that one resolves ${env:...} references and would raise here for a
        missing credential, which is McpCredentialCheck's finding to report.
        """
        attached = [server for server in mcp_servers.values() if not server.disabled]
        if not attached:
            return cls.capabilities
        return dataclasses.replace(
            cls.capabilities, tool_restriction=ToolRestriction.SERVER_SCOPED_ALLOWLIST
        )

    def capabilities_for(self, request: EngineRequest) -> EngineCapabilities:
        # "no tools" needs its own state rather than an empty list (sca-0a7),
        # so the axis comes from the request -- but only once the command
        # built for it actually carries the closing argument.
        mcp_servers = _claude_mcp_servers(self.profile.mcp_servers)
        args = self._tool_args(request, mcp_servers)
        closed = "--tools" in args or "*" in args
        restriction = request.tools.restriction if closed else ToolRestriction.NONE
        if restriction is ToolRestriction.EXACT_ALLOWLIST and _open_mcp_servers(
            request.tools.names, mcp_servers
        ):
            # An open server also carries the tools the allowlist never named.
            # The block cuts at the server, so that is the unit actually held
            # and the axis says so rather than claiming tool granularity
            # (sca-vo05).
            restriction = ToolRestriction.SERVER_SCOPED_ALLOWLIST
        return dataclasses.replace(self.capabilities, tool_restriction=restriction)

    def footprint_for(self, request: EngineRequest) -> PayloadFootprint:
        # 붙인 MCP 서버 수를 감사 payload 기록에 남긴다. 서버 정의를 풀지
        # 않고 이름만 세므로 자격증명이 없어도 실패하지 않는다.
        base = super().footprint_for(request)
        names = [name for name, server in self.profile.mcp_servers.items() if not server.disabled]
        tools = _closed_tool_names(
            self._tool_args(request, _claude_mcp_servers(self.profile.mcp_servers))
        )
        return dataclasses.replace(
            base, mcp_servers_attached=len(_attached_mcp_servers(request, names)), **tools
        )

    def prepare(self, request: EngineRequest) -> None:
        self._ensure_skill_discovery_link()

    def _ensure_skill_discovery_link(self) -> None:
        paths = self.profile.paths
        paths.skill_files.mkdir(parents=True, exist_ok=True)
        link = paths.skills / ".claude" / "skills"
        if link.is_symlink():
            if link.resolve() != paths.skill_files.resolve():
                link.unlink()
            else:
                return
        elif link.exists():
            # A real directory here holds someone's skill files. Replacing it
            # would delete them, so it is left alone and only logged.
            log.warning(
                "스킬 탐색 경로가 symlink 가 아니라 실제 디렉터리다 : %s - "
                "봇이 이 자리에 스킬을 쓸 수 없다. 내용을 %s 로 옮기고 이 디렉터리를 지운다",
                link, paths.skill_files,
            )
            return
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(paths.skill_files, target_is_directory=True)

    def build_command(self, request: EngineRequest) -> list[str]:
        # Don't pass --max-budget-usd. A subscription OAuth token
        # (sk-ant-oat) rejects even normal requests if given that flag
        # — subscriptions spend a seat allowance, not a usage-based
        # budget, so a dollar cap doesn't apply on this billing path.
        cmd: list[str] = [
            str(self.spec.binary),
            "-p",
            "--output-format", "json",
            # Without this the operator's own ~/.claude/settings.json rides
            # along: its permissions.allow, its env block and its hooks all
            # apply to the bot's turn. The bot's own file goes in through
            # --settings instead (bot.py:1343).
            #
            # Empty rather than the original's "project": a channel's work
            # directory can be any checkout, and a .claude/settings.json
            # there set an env var inside the bot's own tool call (measured
            # 2026-09-24). Nothing the bot needs comes from that source --
            # persona, knowledge, skills and MCP all arrive as arguments.
            "--setting-sources", "",
            "--permission-mode", "dontAsk",
            "--model", request.require_model(),
            "--effort", self.resolve_effort(request),
        ]
        mcp_servers = _claude_mcp_servers(
            self.profile.mcp_servers,
            can_act=request.tools.can_act,
            is_owner=acts_for_owner(request),
            turn_context=request.turn_context,
        )
        attached = set(_attached_mcp_servers(request, mcp_servers))
        # 서버 자리 와일드카드 이름은 붙인 서버 기준으로 다시 펴야 하므로
        # 거부 목록 계산은 붙이기 전 전체 목록으로 한다. 붙이지 않은 서버의
        # 거부 패턴은 남아도 해가 없다.
        cmd += self._tool_args(request, mcp_servers)
        cmd += self._agent_args(request)
        mcp_servers = {name: entry for name, entry in mcp_servers.items() if name in attached}
        for path in request.readable_dirs:
            cmd += ["--add-dir", str(path)]
        skills_dir = self.profile.paths.skills
        if skills_dir.exists():
            cmd += ["--add-dir", str(skills_dir)]
        if mcp_servers:
            cmd += ["--mcp-config", json.dumps({"mcpServers": mcp_servers}, ensure_ascii=False)]
        # Unconditional: it used to ride along with --mcp-config, so a profile
        # with no servers took the user's global ones instead -- 9 of them at
        # the 2026-09-20 measurement. With the built-in tools closed the model
        # reached for one of those anyway (sca-mo4g).
        cmd += ["--strict-mcp-config"]
        settings = self._settings_value(request)
        if settings:
            cmd += ["--settings", json.dumps(settings, ensure_ascii=False)]
        cmd += ["--append-system-prompt", request.system_prompt]
        cmd += (["--resume", request.require_session_id()] if request.resume
                else ["--session-id", request.require_session_id()])
        # Keep the prompt after --. Otherwise user input starting
        # with a hyphen would be parsed as a flag.
        cmd += ["--", request.prompt]
        return cmd

    #: The overlay a non-owner request gets instead of `general`'s once a
    #: write-capable tool is already in its allowlist. Named after the channel
    #: flag that is the only way that can happen (auth/policy.py's
    #: OPERATOR_TOOLS gate on ChannelConfig.operator_full) -- if the name axis
    #: let Bash/Edit/Write/NotebookEdit through, the settings axis must not
    #: turn around and deny it, or naming a tool and actually getting it
    #: disagree again (the 2026-10-02~10-14 mametchi bug, gaps_D 3절).
    OPERATOR_FULL_SETTINGS_LEVEL: ClassVar[str] = "operator_full"

    def _settings_value(self, request: EngineRequest) -> dict[str, Any]:
        """Everything --settings has to carry this turn, as one document.

        The flag takes a single value, so the bot's own settings, the
        overlay for this trust level and the per-request progress hook are
        merged here rather than passed as several sources.

        The original keeps one whole file per trust level (bot.py:1345).
        An overlay instead: its owner and trusted files hold the same
        entries, so two copies had to be kept in step by hand.
        """
        paths = self.profile.paths
        level = request.trust_level.name.lower()
        if request.delegated:
            # 위임 턴은 소유자 도구로 돈다. general 오버레이의 Bash/Edit/Write deny 를
            # 싣지 않는다. 원장 쓰기 deny 는 아래에서 그대로 걸린다.
            level = TrustLevel.OWNER.name.lower()
        if level == TrustLevel.GENERAL.name.lower() and self._operator_full_signal(request):
            level = self.OPERATOR_FULL_SETTINGS_LEVEL
        # The deny check reads the merged document, so the files go in
        # together and the progress hook is layered on after: that fragment
        # is per request and carries no permissions (sca-e34p).
        merged = load_merged_settings(
            paths.engine_settings(self.name),
            paths.engine_settings(f"{self.name}.{level}"),
        )
        merged = _lift_scoped_denials(merged, request.tools.names)
        if request.trust_level is not TrustLevel.OWNER:
            merged = _deny_ledger_writes(
                merged, self.profile.delegation_ledger, self.profile.delegation_record,
            )
        if request.progress_log is not None:
            merged = merge_settings(merged, progress_hook_settings(request.progress_log))
        # Always on, regardless of progress_log: these are engine built-ins,
        # not a per-request feature toggle (sca-kos.B4).
        return merge_settings(merged, guard_hook_settings(), NO_ATTRIBUTION_SETTINGS)

    @staticmethod
    def _operator_full_signal(request: EngineRequest) -> bool:
        """True when this GENERAL request's tool list already carries a bare
        write-capable tool name.

        For a non-owner, that only happens when AccessPolicy.channel_tools_for
        granted it -- which it only does for a channel with `operator_full` on
        (auth/policy.py's OPERATOR_TOOLS filter). So this is a reliable proxy
        for "operator_full channel, named operator" without engine/claude.py
        having to see the channel config itself: EngineRequest carries no
        channel or principal, only the tool list and trust level the pipeline
        already resolved.
        """
        return not OPERATOR_TOOLS.isdisjoint(request.tools.names)

    #: 2026-09-19 실측 (claude 2.1.270 네이티브 바이너리) - 로그인이 풀리면
    #: 'Please run /login' 을, 인증 자체가 실패하면 'Failed to authenticate' 를
    #: 낸다. 같은 판정에 쓰이는 문구 중 'credit balance' 와 'usage limit' 은
    #: 한도 계열이라 뺐고, MCP 쪽 401 도 여기 해당하지 않는다.
    #: 문구는 좁게 잡는다. 'invalid api key' 나 'not logged in' 같은 조각은
    #: gh·aws 같은 도구도 내므로, 그것으로 판정하면 CLI 로그인은 멀쩡한데
    #: 1차가 통째로 내려간다.
    #: 2026-11-07 마메치 운영 로그의 만료 401 239줄은 모두 'Failed to
    #: authenticate. API Error: 401 OAuth access token has expired.' 였다. 같은
    #: 상태를 만료·폐기 문구만으로 내는 응답도 잡도록 OAuth 문구 둘을 더한다.
    #: 'OAuth' 로 시작하는 전체 구절이라 gh·aws 의 짧은 인증 오류와 겹치지 않는다.
    AUTH_FAILURE_MARKERS: ClassVar[tuple[str, ...]] = (
        "Please run /login",
        "Failed to authenticate.",
        "OAuth access token has expired",
        "OAuth token has been revoked",
    )
    AUTH_FAILURE_NOTE = "claude 로그인이 풀렸다. claude 에서 /login 으로 다시 로그인해야 한다."
    #: result 이벤트의 api_error_status 는 claude 자신의 API 호출 결과다. MCP
    #: 서버의 401 은 이 필드에 오지 않으므로 문구 없이 401 만 와도 만료로 본다.
    AUTH_FAILURE_API_STATUS: ClassVar[int | None] = 401

    def _api_error_status(self, response: EngineResponse) -> int | None:
        """is_error 경로는 raw 에 필드가 있고, 종료코드가 0 이 아니면 stdout 의
        result 이벤트에 있다 (2026-11-07 운영 로그는 종료코드 1 이었다)."""
        found = super()._api_error_status(response)
        if found is not None:
            return found
        raw = response.raw if isinstance(response.raw, Mapping) else {}
        stdout = raw.get("stdout")
        payload = self._result_event(stdout) if isinstance(stdout, str) and stdout else None
        if payload is None:
            return None
        value = payload.get("api_error_status")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def new_session_id(self) -> str:
        return str(uuid.uuid4())

    def parse(self, stdout: str, stderr: str, returncode: int) -> EngineResponse:
        if returncode != 0:
            limit = self._limit_from_text(stdout)
            if limit is not None:
                return EngineResponse(
                    ok=False, body=self._limit_body(limit), session_id=None,
                    model_actual=None, elapsed=0.0, turns=None, usage=None,
                    user_facing=True,
                    raw={"usage_limit": {"detail": limit.detail, "source": limit.source}},
                    failure_reason="usage_limit",
                    failure_detail=FailureDetail(exit_code=returncode, code=limit.source),
                )
            # A run that exited nonzero after spending tokens still prints its
            # result event (2026-11-07 운영 로그, 종료코드 1). Its per-model
            # table is kept so the spend reaches the audit row (A14).
            failed_event = self._result_event(stdout) if stdout else None
            return EngineResponse(
                ok=False, body="처리에 실패했습니다.", session_id=None, model_actual=None,
                elapsed=0.0, turns=None, usage=None,
                model_usage=_model_usage(failed_event) if failed_event is not None else None,
                raw={"stdout": stdout, "stderr": stderr, "returncode": returncode},
                failure_reason="nonzero_exit", failure_detail=FailureDetail(exit_code=returncode),
            )

        payload = self._result_event(stdout)
        if payload is None:
            return EngineResponse(
                ok=False, body="처리에 실패했습니다.", session_id=None, model_actual=None,
                elapsed=0.0, turns=None, usage=None,
                raw={"stdout": stdout}, failure_reason="bad_json",
                failure_detail=FailureDetail(stdout_chars=len(stdout)),
            )

        session_id = payload.get("session_id")
        model_actual = _actual_model(payload)
        turns = payload.get("num_turns")
        usage = Usage.from_native(payload.get("usage"), _USAGE_KEY_MAP)
        model_usage = _model_usage(payload)
        elapsed, elapsed_source = self._elapsed_from_payload(payload)

        if payload.get("is_error"):
            subtype = str(payload.get("subtype") or "")
            # 종료코드 0 에 subtype success 로 오고 한도는 api_error_status 429 나
            # result 문구에만 있는 경우가 있다. 종료코드가 0 이 아닐 때만 문구를
            # 보면 이 경우가 일반 실패로 떨어져 2차 전환이 일어나지 않는다.
            text_limit = self._limit_from_payload(payload)
            is_limit = text_limit is not None
            reason = "usage_limit" if is_limit else "is_error"
            body = (self._limit_body(text_limit) if text_limit is not None
                    else "처리에 실패했습니다.")
            raw: dict[str, Any] = dict(payload)
            if text_limit is not None:
                raw["usage_limit"] = {"detail": text_limit.detail, "source": text_limit.source}
            return EngineResponse(
                ok=False, body=body, session_id=session_id, model_actual=model_actual,
                elapsed=elapsed, turns=turns, usage=usage, model_usage=model_usage,
                raw=raw, failure_reason=reason,
                failure_detail=FailureDetail(code=subtype), elapsed_source=elapsed_source,
                # Only the limit notice is written for the person who asked; the
                # generic failure text adds nothing to the failure mark (sca-5sc).
                user_facing=is_limit,
            )

        body = str(payload.get("result") or "").strip()
        if not body:
            return EngineResponse(
                ok=False, body="응답이 비어 있습니다.", session_id=session_id,
                model_actual=model_actual, elapsed=elapsed, turns=turns, usage=usage,
                model_usage=model_usage,
                raw=dict(payload), failure_reason="empty_response",
                failure_detail=FailureDetail(
                    exit_code=returncode, stdout_chars=len(stdout),
                    code=str(payload.get("subtype") or ""),
                ),
                elapsed_source=elapsed_source,
            )
        return EngineResponse(
            ok=True, body=body, session_id=session_id, model_actual=model_actual,
            elapsed=elapsed, turns=turns, usage=usage, model_usage=model_usage,
            raw=dict(payload), failure_reason=None, elapsed_source=elapsed_source,
        )

    @staticmethod
    def _elapsed_from_payload(payload: Mapping[str, Any]) -> tuple[float, ElapsedSource]:
        """duration_ms confirmed 2026-09-16 against the real CLI's result event."""
        duration_ms = payload.get("duration_ms")
        if isinstance(duration_ms, (int, float)):
            return float(duration_ms) / 1000.0, ElapsedSource.ENGINE
        return 0.0, ElapsedSource.UNKNOWN

    @staticmethod
    def _result_event(stdout: str) -> Mapping[str, Any] | None:
        """The CLI emits either a single result object or an array of events
        ending in one (confirmed 2026-09-15). Both shapes carry the same keys
        on the result itself.
        """
        try:
            payload = json.loads(stdout)
        except (json.JSONDecodeError, TypeError):
            return None
        if isinstance(payload, Mapping):
            return payload
        if isinstance(payload, list):
            for event in reversed(payload):
                if isinstance(event, Mapping) and event.get("type") == "result":
                    return event
        return None

    def detect_usage_limit(self, response: EngineResponse) -> UsageLimit | None:
        info = response.raw.get("usage_limit") if isinstance(response.raw, Mapping) else None
        if not info:
            return None
        return UsageLimit(detail=info.get("detail", ""), source=info.get("source", "hint"))

    @staticmethod
    def _limit_body(limit: UsageLimit) -> str:
        if limit.detail:
            return f"구독 사용 한도에 걸려 지금은 답할 수 없어요.\n\n> {limit.detail}"
        return "구독 사용 한도에 걸려 지금은 답할 수 없어요."

    @staticmethod
    def _limit_from_text(stdout: str) -> UsageLimit | None:
        """Same detection as the original's usage_limit_message().

        Sometimes subtype is "success" and the exit code is 0, but
        api_error_status is 429 and the actual reason only shows up in
        the result text — exit code alone misses it (observed
        2026-09-11).
        """
        try:
            payload = json.loads(stdout or "")
        except (json.JSONDecodeError, TypeError):
            payload = ClaudeEngine._result_event_static(stdout)
        if not isinstance(payload, Mapping):
            return None
        return ClaudeEngine._limit_from_payload(payload)

    @staticmethod
    def _result_event_static(stdout: str) -> Mapping[str, Any] | None:
        """stream-json 출력이면 마지막 result 이벤트를 꺼낸다."""
        for line in reversed((stdout or "").splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, Mapping) and event.get("type") == "result":
                return event
        return None

    @staticmethod
    def _limit_from_payload(payload: Mapping[str, Any]) -> UsageLimit | None:
        result = str(payload.get("result") or "")
        status = payload.get("api_error_status")
        subtype = str(payload.get("subtype") or "")
        low = result.lower()
        if status == 429:
            source = "status_code"
        elif any(hint in low for hint in _USAGE_LIMIT_HINTS):
            source = "hint"
        elif "limit" in subtype or "budget" in subtype:
            source = "subtype"
        else:
            return None
        return UsageLimit(detail=result.strip(), source=source)


#: modelUsage row keys (camelCase, confirmed 2026-10-16 in mametchi
#: compact_model_usage) to the common Usage vocabulary.
_MODEL_USAGE_KEY_MAP: Mapping[str, str] = {
    "input_tokens": "inputTokens",
    "output_tokens": "outputTokens",
    "cache_creation_tokens": "cacheCreationInputTokens",
    "cache_read_tokens": "cacheReadInputTokens",
}


def _model_usage(payload: Mapping[str, Any]) -> dict[str, ModelUsage] | None:
    """The result event's modelUsage table, every model kept (A14).

    _actual_model keeps one model; the advisor and subagent models that ran
    beside it are only visible here, so the whole table is carried to the
    audit record. A row that is not a mapping is skipped, a non-numeric
    value reads as 0, and a missing costUSD stays None rather than 0.
    """
    table = payload.get("modelUsage")
    if not isinstance(table, Mapping) or not table:
        return None
    out: dict[str, ModelUsage] = {}
    for model, row in table.items():
        if not isinstance(row, Mapping):
            continue
        counts = {
            field_name: _int_or_zero(row.get(native))
            for field_name, native in _MODEL_USAGE_KEY_MAP.items()
        }
        cost = row.get("costUSD")
        cost_usd = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
        out[str(model)] = ModelUsage(**counts, cost_usd=cost_usd)
    return out or None


def _int_or_zero(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _actual_model(payload: Mapping[str, Any]) -> str | None:
    """The model that actually spent tokens this run.

    The result event carries no `model` key (measured 2026-09-19 on 2.1.263),
    only modelUsage -- a per-model usage table. A resumed session that switched
    models brings several, so the one with the most output tokens wins
    (bot.py:207). Unknown stays unknown: a single entry is the model that ran
    even at zero tokens, but a tie at the top is not evidence for either, and
    picking by insertion order would record a guess as a fact (codex review).
    """
    table = payload.get("modelUsage")
    if not isinstance(table, dict) or not table:
        return None
    if len(table) == 1:
        return str(next(iter(table)))

    ranked = sorted(table.items(), key=lambda kv: _out_tokens(kv[1]), reverse=True)
    top = _out_tokens(ranked[0][1])
    if top == 0 or top == _out_tokens(ranked[1][1]):
        return None
    return str(ranked[0][0])


def _out_tokens(value: Any) -> int:
    """The table comes from the CLI. A value that is not a number must not
    break a request whose answer already arrived."""
    if not isinstance(value, Mapping):
        return 0
    try:
        return int(value.get("outputTokens") or 0)
    except (TypeError, ValueError):
        return 0
