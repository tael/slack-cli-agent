"""Codex CLI adapter.

Three differences from Claude:

- System prompt: Codex only accepts developer_instructions when
  opening a thread. A new value on a resumed turn is ignored — the
  first value wins (confirmed 2026-09-11). So the system prompt is
  only sent on the first turn, and directives_for_turn() carries only
  what changes per turn.
- Session: the Codex CLI mints its own thread_id, returned by
  session_id_from(). new_session_id() only produces a placeholder used
  until then.
- Prompt: passed after a ``--`` separator. Without it a prompt starting
  with a hyphen is parsed as a flag and the CLI exits with code 2
  (observed 2026-09-15). The official reference documents ``-`` plus
  stdin for this case but is silent on ``--``; ``--`` was confirmed to
  work against the real CLI and is guarded by the real_cli smoke suite.
- Paths: there's no --add-dir equivalent. readable_paths_note() builds
  a sentence appended to the system instructions instead.

MCP servers (sca-kos.2): codex has no --mcp-config-style flag, but `-c`
takes a dotted config path per `codex --help` ("Use a dotted path
(foo.bar.baz) to override nested values"), and learn.chatgpt.com's
config reference documents config.toml's mcp_servers.<id> keys —
command/args/env/cwd for stdio, url plus http_headers for remote,
disabled_tools, and enabled (our disabled, inverted). The http_headers
key name was confirmed against codex-cli 0.154.0 with `codex mcp list
--json` (sca-m7w). Each field becomes its own -c
override, so no config.toml file needs writing for this. Skills have no
equivalent injection point (see engine/base.py's docstring override on
this class, TestSkillDirectoryIsolation's xfail reason in
tests/unit/test_engine_isolation.py): codex only scans .agents/skills
relative to CWD/repo root, or $HOME/.agents/skills — there's no flag to
point it at an arbitrary bot-owned directory.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..config.profile import McpServerSpec
from .base import Engine, EngineRequest, EngineResponse, FailureDetail, Usage, UsageLimit
from .capability import (
    EngineCapabilities,
    ExecutionIsolation,
    InstructionBoundary,
    ToolRestriction,
)

# sca-dyb.4 — confirmed 2026-09-16 against the real CLI's turn.completed
# event: Codex doesn't use Claude's cache_read_input_tokens/
# cache_creation_input_tokens names at all.
#
# Public (not _-prefixed) because engine/transcript.py's CodexTranscriptReader
# reads the same key names out of a different event type (token_usage_record)
# and must not drift from this mapping — see that module's _merge_usage.
CODEX_USAGE_KEY_MAP: Mapping[str, str] = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_tokens": "cached_input_tokens",
    "cache_creation_tokens": "cache_write_input_tokens",
}


def _codex_mcp_config_args(mcp_servers: Mapping[str, McpServerSpec]) -> list[str]:
    args: list[str] = []
    for name, server in mcp_servers.items():
        prefix = f"mcp_servers.{name}"
        if server.disabled:
            args += ["-c", f"{prefix}.enabled=false"]
            continue
        if server.is_remote:
            args += ["-c", f"{prefix}.url={CodexEngine._toml_string(server.url)}"]
            for key, value in server.resolved_headers().items():
                args += ["-c", f"{prefix}.http_headers.{key}={CodexEngine._toml_string(value)}"]
        else:
            args += ["-c", f"{prefix}.command={CodexEngine._toml_string(server.command)}"]
            if server.args:
                args += ["-c", f"{prefix}.args={json.dumps(list(server.args))}"]
            if server.cwd:
                args += ["-c", f"{prefix}.cwd={CodexEngine._toml_string(str(server.cwd))}"]
        for key, value in server.resolved_env().items():
            args += ["-c", f"{prefix}.env.{key}={CodexEngine._toml_string(value)}"]
        if server.disabled_tools:
            args += ["-c", f"{prefix}.disabled_tools={json.dumps(list(server.disabled_tools))}"]
    return args


#: options.sandbox values that mean something is actually enforced.
_SANDBOX_ISOLATION = {
    "read-only": ExecutionIsolation.READONLY_SANDBOX,
    "workspace-write": ExecutionIsolation.WORKSPACE_WRITE,
}


class CodexEngine(Engine):
    name = "codex"

    # developer_instructions is a separate field from the prompt. Isolation is
    # whatever options.sandbox says, which the default (danger-full-access)
    # turns off entirely -- hence capabilities_for below.
    capabilities = EngineCapabilities(
        tool_restriction=ToolRestriction.COARSE_SANDBOX,
        execution_isolation=ExecutionIsolation.READONLY_SANDBOX,
        instruction_boundary=InstructionBoundary.NATIVE,
    )

    def capabilities_for(self, request: EngineRequest) -> EngineCapabilities:
        sandbox = str(self.spec.options.get("sandbox", "danger-full-access"))
        isolation = _SANDBOX_ISOLATION.get(sandbox, ExecutionIsolation.NONE)
        return dataclasses.replace(
            self.capabilities,
            execution_isolation=isolation,
            tool_restriction=(
                ToolRestriction.COARSE_SANDBOX
                if isolation is not ExecutionIsolation.NONE
                else ToolRestriction.NONE
            ),
        )

    def build_command(self, request: EngineRequest) -> list[str]:
        binary = str(self.spec.binary)
        # The bot is meant to read and write freely; a profile can still narrow
        # this with options.sandbox.
        sandbox = str(self.spec.options.get("sandbox", "danger-full-access"))
        cmd: list[str] = [binary, "exec"]
        if request.resume:
            cmd.append("resume")
        cmd += ["--json", "--skip-git-repo-check"]
        if self.spec.options.get("network"):
            # The read-only sandbox blocks network too; the profile decides whether to open it.
            cmd += ["-c", "sandbox_workspace_write.network_access=true"]
        cmd += ["-m", request.require_model()]
        if request.effort:
            cmd += ["-c", f"model_reasoning_effort={self._toml_string(request.effort)}"]
        cmd += _codex_mcp_config_args(self.profile.mcp_servers)
        if not request.resume and request.system_prompt:
            instructions = request.system_prompt + self.readable_paths_note(request.readable_dirs)
            cmd += ["-c", f"developer_instructions={self._toml_string(instructions)}"]

        if request.resume:
            # resume doesn't accept --sandbox or -C; achieve the same effect via config keys instead.
            cmd += ["-c", f"sandbox_mode={self._toml_string(sandbox)}",
                   request.require_session_id(), "--", request.prompt]
        else:
            cmd += ["--sandbox", sandbox, "-C", str(request.workdir), "--", request.prompt]
        return cmd

    def new_session_id(self) -> str:
        # The real thread ID is minted by the CLI on first run. This is
        # only a placeholder until then — on resume it must be
        # replaced with the real value from session_id_from().
        return str(uuid.uuid4())

    def session_id_from(self, response: EngineResponse) -> str | None:
        return response.session_id

    def directives_for_turn(self, request: EngineRequest) -> str:
        if not request.resume:
            return ""
        return f"[신뢰 등급 : {request.trust_level.name}]\n\n"

    def readable_paths_note(self, paths: Sequence[Path]) -> str:
        if not paths:
            return ""
        lines = "\n".join(f"- {path}" for path in paths)
        return (
            "\n\n# 읽을 수 있는 자리\n\n"
            "작업 디렉터리 밖이지만 읽을 수 있는 경로다. 조사할 때 여기를 본다.\n\n"
            f"{lines}\n"
        )

    def parse(self, stdout: str, stderr: str, returncode: int) -> EngineResponse:
        if returncode != 0:
            return EngineResponse(
                ok=False, body="Codex 실행에 실패했습니다.", session_id=None,
                model_actual=None, elapsed=0.0, turns=None, usage=None,
                raw={"stdout": stdout, "stderr": stderr, "returncode": returncode},
                failure_reason="nonzero_exit", failure_detail=FailureDetail(exit_code=returncode),
            )
        try:
            thread_id, text, usage_data, tool_errors = self._parse_jsonl(stdout)
        except (json.JSONDecodeError, ValueError, TypeError):
            return EngineResponse(
                ok=False, body="Codex 응답 형식을 읽지 못했습니다.", session_id=None,
                model_actual=None, elapsed=0.0, turns=None, usage=None,
                raw={"stdout": stdout}, failure_reason="bad_json",
                failure_detail=FailureDetail(stdout_chars=len(stdout)),
            )

        body = (text or "").strip()
        # elapsed stays 0.0/"unknown" — confirmed 2026-09-16 that codex exec
        # --json never emits a duration field on any event. EngineRunner
        # fills it from wall-clock time instead.
        raw = {"thread_id": thread_id, "tool_errors": tool_errors, "usage": usage_data}
        usage = Usage.from_native(usage_data, CODEX_USAGE_KEY_MAP)
        if not body:
            return EngineResponse(
                ok=False, body="Codex 응답이 비어 있습니다.", session_id=thread_id,
                model_actual=None, elapsed=0.0, turns=None,
                usage=usage, raw=raw,
                failure_reason="empty_response",
                failure_detail=FailureDetail(tool_errors=len(tool_errors)),
            )
        return EngineResponse(
            ok=True, body=body, session_id=thread_id, model_actual=None, elapsed=0.0,
            turns=None, usage=usage, raw=raw, failure_reason=None,
        )

    def detect_usage_limit(self, response: EngineResponse) -> UsageLimit | None:
        # Codex has no subscription weekly-limit concept; the original doesn't check for it either.
        return None

    @staticmethod
    def _parse_jsonl(output: str) -> tuple[str | None, str, dict[str, Any], list[str]]:
        thread_id: str | None = None
        text = ""
        usage: dict[str, Any] = {}
        tool_errors: list[str] = []
        for line in output.splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict):
                raise TypeError("JSONL 이벤트가 객체가 아니다")
            kind = event.get("type")
            if kind == "thread.started":
                thread_id = event.get("thread_id")
            elif kind == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                    text = item["text"]
                elif item.get("type") == "error" and isinstance(item.get("message"), str):
                    tool_errors.append(item["message"])
            elif kind == "turn.completed":
                event_usage = event.get("usage")
                if isinstance(event_usage, dict):
                    usage = event_usage
        return thread_id, text, usage, tool_errors

    @staticmethod
    def _toml_string(value: str) -> str:
        """``-c key=value``'s value parses as TOML; JSON string escaping
        matches TOML's basic string escaping, so it's safe to reuse."""
        return json.dumps(value, ensure_ascii=False)
