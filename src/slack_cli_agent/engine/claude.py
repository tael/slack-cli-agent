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
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

from ..config.profile import McpServerSpec
from .base import (
    ElapsedSource,
    Engine,
    EngineRequest,
    EngineResponse,
    FailureDetail,
    Usage,
    UsageLimit,
)

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
_USAGE_KEY_MAP: Mapping[str, str] = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_creation_tokens": "cache_creation_input_tokens",
    "cache_read_tokens": "cache_read_input_tokens",
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
                entry["headers"] = dict(server.headers)
        else:
            entry = {"command": server.command}
            if server.args:
                entry["args"] = list(server.args)
            if server.env:
                entry["env"] = dict(server.env)
            if server.cwd:
                entry["cwd"] = str(server.cwd)
        servers[name] = entry
    return servers


class ClaudeEngine(Engine):
    name = "claude"

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
            # --allowedTools overrides settings.json's allow rules
            # (confirmed by testing). The read-only permission model is
            # enforced by this list — it's the caller's job (outside
            # this module) to keep Bash/Edit/Write/NotebookEdit out of
            # request.allowed_tools.
            "--allowedTools", ",".join(request.allowed_tools),
            "--model", request.require_model(),
            "--effort", request.effort,
        ]
        for path in request.readable_dirs:
            cmd += ["--add-dir", str(path)]
        skills_dir = self.profile.paths.skills
        if skills_dir.exists():
            cmd += ["--add-dir", str(skills_dir)]
        mcp_servers = _claude_mcp_servers(self.profile.mcp_servers)
        if mcp_servers:
            cmd += [
                "--mcp-config", json.dumps({"mcpServers": mcp_servers}, ensure_ascii=False),
                "--strict-mcp-config",
            ]
        cmd += ["--append-system-prompt", request.system_prompt]
        cmd += (["--resume", request.require_session_id()] if request.resume
                else ["--session-id", request.require_session_id()])
        # Keep the prompt after --. Otherwise user input starting
        # with a hyphen would be parsed as a flag.
        cmd += ["--", request.prompt]
        return cmd

    def new_session_id(self) -> str:
        return str(uuid.uuid4())

    def parse(self, stdout: str, stderr: str, returncode: int) -> EngineResponse:
        if returncode != 0:
            limit = self._limit_from_text(stdout)
            if limit is not None:
                return EngineResponse(
                    ok=False, body=self._limit_body(limit), session_id=None,
                    model_actual=None, elapsed=0.0, turns=None, usage=None,
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
        model_actual = payload.get("model")
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
