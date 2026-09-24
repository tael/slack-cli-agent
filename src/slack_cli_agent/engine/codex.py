"""Codex CLI adapter.

Three differences from Claude:

- System prompt: Codex only accepts developer_instructions when
  opening a thread. A new value on a resumed turn is ignored — the
  first value wins (confirmed 2026-09-11). So the system prompt is
  only sent on the first turn, and a resumed turn carries this turn's
  value inside the prompt string instead (_resume_prompt, sca-ivs).
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
from typing import Any, ClassVar

from ..auth.tools import READ_ONLY_TOOLS
from ..config.profile import McpServerSpec
from .base import (
    UNTRUSTED_INPUT_MARK,
    Engine,
    EngineRequest,
    EngineResponse,
    FailureDetail,
    Usage,
    UsageLimit,
    json_object_line,
)
from .capability import (
    EngineCapabilities,
    ExecutionIsolation,
    InstructionBoundary,
    ToolRestriction,
)
from .footprint import (
    INSTRUCTION_TRANSPORT_USER_PROMPT,
    PayloadFootprint,
    utf8_bytes,
)
from .tool_selection import ToolAccess

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


@dataclasses.dataclass(frozen=True)
class _CodexStream:
    """What one `codex exec --json` run left on stdout."""

    thread_id: str | None
    text: str
    usage: dict[str, Any]
    tool_errors: list[str]
    malformed: bool


#: options.sandbox values that mean something is actually enforced.
_SANDBOX_ISOLATION = {
    "read-only": ExecutionIsolation.READONLY_SANDBOX,
    "workspace-write": ExecutionIsolation.WORKSPACE_WRITE,
}


class CodexEngine(Engine):
    name = "codex"
    shell_always_attached = True

    # developer_instructions is a separate field from the prompt. Isolation is
    # whatever options.sandbox says, which the default (danger-full-access)
    # turns off entirely -- hence capabilities_for below.
    capabilities = EngineCapabilities(
        tool_restriction=ToolRestriction.COARSE_SANDBOX,
        execution_isolation=ExecutionIsolation.READONLY_SANDBOX,
        instruction_boundary=InstructionBoundary.NATIVE,
    )

    def _sandbox_for(self, request: EngineRequest) -> str:
        """The sandbox this turn actually runs under.

        One place, because build_command and capabilities_for both need the
        answer: a declaration the command does not match makes the audit
        record point at something that did not run.

        A turn whose allowlist holds only read tools comes down to read-only
        whatever the profile asks for. codex takes no tool argument, so the
        allowlist would otherwise reach it as a sentence the model may
        ignore; the sandbox is the OS refusing the write (sca-f9k0).
        """
        if request.tools.access is ToolAccess.ALLOWLIST and all(
            name in READ_ONLY_TOOLS for name in request.tools.names
        ):
            return "read-only"
        return str(self.spec.options.get("sandbox", "danger-full-access"))

    def capabilities_for(self, request: EngineRequest) -> EngineCapabilities:
        sandbox = self._sandbox_for(request)
        isolation = _SANDBOX_ISOLATION.get(sandbox, ExecutionIsolation.NONE)
        return dataclasses.replace(
            self.capabilities,
            execution_isolation=isolation,
            # A resumed turn carries this turn's instructions inside the prompt
            # string, next to the Slack input. The CLI keeps the first turn's
            # developer_instructions whatever we pass (measured 2026-09-19), so
            # that layer is no longer where this turn's instructions live
            # (sca-ivs).
            instruction_boundary=(
                InstructionBoundary.PROMPT_ONLY if request.resume
                else InstructionBoundary.NATIVE
            ),
            tool_restriction=(
                ToolRestriction.COARSE_SANDBOX
                if isolation is not ExecutionIsolation.NONE
                else ToolRestriction.NONE
            ),
        )

    def footprint_for(self, request: EngineRequest) -> PayloadFootprint:
        """A resumed turn carries the instructions in the prompt (sca-ivs), so
        those bytes join the session context and are paid again every turn."""
        base = super().footprint_for(request)
        if not request.resume:
            # The ban rides on the turn prompt, not on the session-scoped
            # instructions, so it is counted separately from the path note.
            return dataclasses.replace(base, adapter_added_bytes=utf8_bytes(
                self._session_path_note(request) + self._turn_constraint_prefix(request),
            ))
        return dataclasses.replace(
            base,
            adapter_added_bytes=base.adapter_added_bytes + utf8_bytes(UNTRUSTED_INPUT_MARK),
            instruction_transport=INSTRUCTION_TRANSPORT_USER_PROMPT,
            instruction_replayed_on_resume=True,
        )

    def blocks_outbound_writes(self, request: EngineRequest) -> bool:
        if not super().blocks_outbound_writes(request):
            return False
        # sandbox_workspace_write.network_access only lifts the workspace-write
        # sandbox; read-only stays closed whatever the profile asks for.
        if self.capabilities_for(request).execution_isolation is ExecutionIsolation.WORKSPACE_WRITE:
            return not self.spec.options.get("network")
        return True

    def build_command(self, request: EngineRequest) -> list[str]:
        binary = str(self.spec.binary)
        sandbox = self._sandbox_for(request)
        cmd: list[str] = [binary, "exec"]
        if request.resume:
            cmd.append("resume")
        cmd += ["--json", "--skip-git-repo-check"]
        if self.spec.options.get("network"):
            # The read-only sandbox blocks network too; the profile decides whether to open it.
            cmd += ["-c", "sandbox_workspace_write.network_access=true"]
        cmd += ["-m", request.require_model()]
        # Always sent, never omitted: leaving it out lets the CLI pick its own
        # default while the audit records the requested value (sca-3kzk).
        cmd += ["-c", f"model_reasoning_effort={self._toml_string(self.resolve_effort(request))}"]
        cmd += _codex_mcp_config_args(self.profile.mcp_servers)
        if not request.resume:
            instructions = request.system_prompt + self._session_path_note(request)
            if instructions:
                cmd += ["-c", f"developer_instructions={self._toml_string(instructions)}"]

        if request.resume:
            # resume doesn't accept --sandbox or -C; achieve the same effect via config keys instead.
            cmd += ["-c", f"sandbox_mode={self._toml_string(sandbox)}",
                   request.require_session_id(), "--", self._resume_prompt(request)]
        else:
            cmd += ["--sandbox", sandbox, "-C", str(request.workdir), "--",
                    self._turn_constraint_prefix(request) + request.prompt]
        return cmd

    def _session_path_note(self, request: EngineRequest) -> str:
        """The path note rides on developer_instructions, which is the only
        place a first turn can carry it -- codex's --add-dir adds writable paths,
        not readable ones (codex-cli 0.154.0). It is
        written even with no system prompt: the review path opens its session
        that way (core/application.py:1309) and would otherwise be told
        nothing about where it may read (sca-9u18)."""
        return self.readable_paths_note(request.readable_dirs) + self.write_paths_note(request)

    def _turn_constraint_prefix(self, request: EngineRequest) -> str:
        """The tool ban on a first turn.

        It cannot go in developer_instructions: that value is fixed for the
        session, so a banned first turn would keep banning turns that allow
        tools. The mark comes with it -- without one the constraint would sit
        next to the Slack input with no boundary between them (sca-97n).

        Carries the allowlist wording too: codex has no tool argument, so this
        is the only place an ALLOWLIST request reaches the model (sca-f9k0).
        """
        note = self.tool_ban_note(request) + self.tool_allow_note(request)
        return f"{note}{UNTRUSTED_INPUT_MARK}" if note else ""

    def _resume_prompt(self, request: EngineRequest) -> str:
        """Carries this turn's instructions in the prompt on a resumed turn.

        developer_instructions is pinned to the session's first turn: passing a
        new value on resume leaves the original in force (measured 2026-09-19).
        Without this the bot would answer a resumed turn with instructions from
        whenever the thread started -- a stale run id, prompt files edited since,
        knowledge picked for a different question (sca-ivs). gemini puts the same
        two parts in one string for the same reason.

        The mark goes in even with no instructions: the review path resumes with
        an empty system prompt, and dropping it there would leave that one path
        without the untrusted-input marker.
        """
        return (
            request.system_prompt
            + self.readable_paths_note(request.readable_dirs)
            + self.write_paths_note(request)
            + self.tool_ban_note(request)
            + self.tool_allow_note(request)
            + UNTRUSTED_INPUT_MARK
            + request.prompt
        )

    # 2026-09-19 실측 — item.started/item.completed 의 item.type 이 도구 이름
    # 자리다(command_execution, file_change, web_search, agent_message).
    # started 와 completed 가 잇달아 같은 이름을 내지만 ProgressLogReader 가
    # 연속 중복을 합친다.
    streams_progress = True

    def progress_tool_name(self, line: str) -> str:
        event = json_object_line(line)
        if event is None or event.get("type") not in ("item.started", "item.completed"):
            return ""
        item = event.get("item")
        if not isinstance(item, Mapping):
            return ""
        kind = item.get("type")
        return kind if isinstance(kind, str) else ""

    def new_session_id(self) -> str:
        # The real thread ID is minted by the CLI on first run. This is
        # only a placeholder until then — on resume it must be
        # replaced with the real value from session_id_from().
        return str(uuid.uuid4())

    def session_id_from(self, response: EngineResponse) -> str | None:
        return response.session_id


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
        # Read first even when the run failed: the thread ID and the tokens
        # already spent are the CLI's, not ours, and dropping them leaves the
        # slow-request report looking for a rollout file under a provisional
        # ID that names no transcript (core/pipeline.py:197) (sca-biud).
        stream = self._parse_jsonl(stdout)
        thread_id, text, usage_data, tool_errors = (
            stream.thread_id, stream.text, stream.usage, stream.tool_errors
        )
        salvaged = Usage.from_native(usage_data, CODEX_USAGE_KEY_MAP) if usage_data else None
        if returncode != 0:
            return EngineResponse(
                ok=False, body="Codex 실행에 실패했습니다.", session_id=thread_id,
                model_actual=None, elapsed=0.0, turns=None, usage=salvaged,
                raw={"stdout": stdout, "stderr": stderr, "returncode": returncode},
                failure_reason="nonzero_exit", failure_detail=FailureDetail(exit_code=returncode),
            )
        if stream.malformed:
            return EngineResponse(
                ok=False, body="Codex 응답 형식을 읽지 못했습니다.", session_id=thread_id,
                model_actual=None, elapsed=0.0, turns=None, usage=salvaged,
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

    #: 토큰이 폐기됐을 때만 나오는 문구다. CLI 는 인증 실패도 다른 실패도
    #: 종료코드 1 로 내서 코드로는 못 가린다. 401 이라는 상태 코드만으로
    #: 좁히지 않는 이유는 MCP 서버 하나가 401 을 내도 codex 자체의 로그인은
    #: 멀쩡하기 때문이다 (2026-09-19 실측, asuka 프로필).
    AUTH_FAILURE_MARKERS: ClassVar[tuple[str, ...]] = (
        "refresh_token_invalidated",
        "token_revoked",
        "Please log out and sign in again",
        "Your session has ended. Please log in again",
    )
    AUTH_FAILURE_NOTE = "codex 로그인이 풀렸다. codex login 으로 다시 로그인해야 한다."

    @staticmethod
    def _parse_jsonl(output: str) -> _CodexStream:
        """Reads as far as the stream allows and says whether anything was
        unreadable, rather than raising. A run cut off mid-write leaves a
        truncated last line, and that is exactly when the events before it
        are worth keeping."""
        thread_id: str | None = None
        text = ""
        usage: dict[str, Any] = {}
        tool_errors: list[str] = []
        malformed = False
        for line in output.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                malformed = True
                continue
            if not isinstance(event, dict):
                malformed = True
                continue
            kind = event.get("type")
            if kind == "thread.started":
                started = event.get("thread_id")
                if isinstance(started, str):
                    thread_id = started
                elif started is not None:
                    # A non-string ID would fail further down, where the type
                    # says it cannot happen (codex review).
                    malformed = True
            elif kind == "item.completed":
                item = event.get("item")
                if item is not None and not isinstance(item, Mapping):
                    malformed = True
                    continue
                item = item or {}
                if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                    text = item["text"]
                elif item.get("type") == "error" and isinstance(item.get("message"), str):
                    tool_errors.append(item["message"])
            elif kind == "turn.completed":
                event_usage = event.get("usage")
                if isinstance(event_usage, dict):
                    usage = event_usage
        return _CodexStream(thread_id, text, usage, tool_errors, malformed)

    @staticmethod
    def _toml_string(value: str) -> str:
        """``-c key=value``'s value parses as TOML; JSON string escaping
        matches TOML's basic string escaping, so it's safe to reuse."""
        return json.dumps(value, ensure_ascii=False)
