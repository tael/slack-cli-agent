"""Claude Code CLI adapter.

Paths go through --add-dir. We mint the session ID and pass it via
--session-id; continuing turns pass the same value via --resume. The
system prompt is resent every turn via --append-system-prompt — Claude
refreshes it each turn, so directives_for_turn() isn't needed here (it
keeps the default empty string).

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
import json
import logging
import shlex
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

from ..config.profile import McpServerSpec
from ..observability.progress_hook import HOOK_SCRIPT
from .base import (
    ElapsedSource,
    Engine,
    EngineRequest,
    EngineResponse,
    FailureDetail,
    Usage,
    UsageLimit,
)
from .capability import (
    EngineCapabilities,
    ExecutionIsolation,
    InstructionBoundary,
    ToolRestriction,
)
from .tool_selection import ToolAccess

# Same hint list as the original bot.py's usage_limit_message().
_USAGE_LIMIT_HINTS = (
    "weekly limit", "usage limit", "rate limit",
    "hit your limit", "limit · resets", "limit reached",
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


def _builtin_names(names: tuple[str, ...]) -> list[str]:
    """The built-in tools among what the caller allowed.

    --tools takes names from the built-in set. MCP tools are not in it, so
    they ride on --allowedTools alone. An empty result is still passed: dropping the argument opens every
    built-in tool, which is the opposite of an allowlist naming none.
    """
    return [name for name in names if not name.startswith(MCP_PREFIX)]


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


def _claude_mcp_servers(mcp_servers: Mapping[str, McpServerSpec]) -> dict[str, Any]:
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
            if server.env:
                entry["env"] = server.resolved_env()
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

    def capabilities_for(self, request: EngineRequest) -> EngineCapabilities:
        # What the caller asked for maps straight onto this axis. An empty
        # --allowedTools is not a restriction the caller placed, which is why
        # "no tools" needs its own state rather than an empty list (sca-0a7).
        return dataclasses.replace(
            self.capabilities, tool_restriction=request.tools.restriction,
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
            "--permission-mode", "dontAsk",
            "--model", request.require_model(),
            "--effort", request.effort,
        ]
        if request.tools.access is ToolAccess.FORBIDDEN:
            # The wildcard empties the tool set itself: the debug log stops
            # loading tools and tool_use never appears (2026-09-19 measurement,
            # sca-0a7). No --allowedTools beside it, so the command matches
            # what was measured -- and the wildcard wins over it anyway.
            cmd += ["--disallowedTools", "*"]
        elif request.tools.access is ToolAccess.ALLOWLIST:
            # --allowedTools only adds auto-approval; it does not close the
            # tools left out of it. Measured 2026-09-20 (claude 2.1.263):
            # --allowedTools "Read" still ran Bash, with and without
            # --setting-sources project and an empty permissions file. The same
            # path with deny in it removed the tool, so the file was read.
            # --tools is what closes them: it names the built-in set itself,
            # and a Write call under --tools "Read,Grep,Glob" came back as
            # "No such tool available" (sca-6ewc).
            cmd += ["--allowedTools", ",".join(request.tools.names)]
            cmd += ["--tools", ",".join(_builtin_names(request.tools.names))]
            if not any(name.startswith(MCP_PREFIX) for name in request.tools.names):
                # --tools covers the built-in set only, so a profile's MCP
                # tools stay on the model's list whatever the allowlist says.
                # dontAsk refuses them at approval time, but that is a
                # permission rule -- a name in --allowedTools or in settings'
                # allow opens it. Measured 2026-09-20: with this added, a turn
                # asked what it had answered Glob, Grep, Read and nothing else
                # (sca-mo4g). Not added when the allowlist names an MCP tool:
                # the pattern would close that one too.
                cmd += ["--disallowedTools", f"{MCP_PREFIX}*"]
        for path in request.readable_dirs:
            cmd += ["--add-dir", str(path)]
        skills_dir = self.profile.paths.skills
        if skills_dir.exists():
            cmd += ["--add-dir", str(skills_dir)]
        mcp_servers = _claude_mcp_servers(self.profile.mcp_servers)
        if mcp_servers:
            cmd += ["--mcp-config", json.dumps({"mcpServers": mcp_servers}, ensure_ascii=False)]
        # Unconditional: it used to ride along with --mcp-config, so a profile
        # with no servers took the user's global ones instead -- 9 of them at
        # the 2026-09-20 measurement. With the built-in tools closed the model
        # reached for one of those anyway (sca-mo4g).
        cmd += ["--strict-mcp-config"]
        if request.progress_log is not None:
            cmd += [
                "--settings",
                json.dumps(progress_hook_settings(request.progress_log), ensure_ascii=False),
            ]
        cmd += ["--append-system-prompt", request.system_prompt]
        cmd += (["--resume", request.require_session_id()] if request.resume
                else ["--session-id", request.require_session_id()])
        # Keep the prompt after --. Otherwise user input starting
        # with a hyphen would be parsed as a flag.
        cmd += ["--", request.prompt]
        return cmd

    #: 2026-09-19 실측 (claude 2.1.270 네이티브 바이너리) - 로그인이 풀리면
    #: 'Please run /login' 을, 인증 자체가 실패하면 'Failed to authenticate' 를
    #: 낸다. 같은 판정에 쓰이는 문구 중 'credit balance' 와 'usage limit' 은
    #: 한도 계열이라 뺐고, MCP 쪽 401 도 여기 해당하지 않는다.
    #: 문구는 좁게 잡는다. 'invalid api key' 나 'not logged in' 같은 조각은
    #: gh·aws 같은 도구도 내므로, 그것으로 판정하면 CLI 로그인은 멀쩡한데
    #: 1차가 통째로 내려간다.
    AUTH_FAILURE_MARKERS: ClassVar[tuple[str, ...]] = (
        "Please run /login",
        "Failed to authenticate.",
    )
    AUTH_FAILURE_NOTE = "claude 로그인이 풀렸다. claude 에서 /login 으로 다시 로그인해야 한다."

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
            return EngineResponse(
                ok=False, body="처리에 실패했습니다.", session_id=None, model_actual=None,
                elapsed=0.0, turns=None, usage=None,
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
        elapsed, elapsed_source = self._elapsed_from_payload(payload)

        if payload.get("is_error"):
            subtype = str(payload.get("subtype") or "")
            is_limit = "limit" in subtype or "budget" in subtype
            reason = "usage_limit" if is_limit else "is_error"
            body = ("사용 한도에 걸려 중단했습니다." if is_limit
                    else "처리에 실패했습니다.")
            raw: dict[str, Any] = dict(payload)
            if is_limit:
                raw["usage_limit"] = {"detail": "", "source": "subtype"}
            return EngineResponse(
                ok=False, body=body, session_id=session_id, model_actual=model_actual,
                elapsed=elapsed, turns=turns, usage=usage, raw=raw, failure_reason=reason,
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
                raw=dict(payload), failure_reason="empty_response",
                failure_detail=FailureDetail(
                    exit_code=returncode, stdout_chars=len(stdout),
                    code=str(payload.get("subtype") or ""),
                ),
                elapsed_source=elapsed_source,
            )
        return EngineResponse(
            ok=True, body=body, session_id=session_id, model_actual=model_actual,
            elapsed=elapsed, turns=turns, usage=usage, raw=dict(payload), failure_reason=None,
            elapsed_source=elapsed_source,
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
            return None
        if not isinstance(payload, Mapping):
            return None
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
