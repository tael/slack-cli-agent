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

from ..auth.principal import TrustLevel
from ..auth.tools import SKILL_TOOL, is_read_only_tool
from ..config.profile import EngineSpec, McpServerSpec
from .base import (
    Engine,
    EngineRequest,
    EngineResponse,
    FailureDetail,
    Usage,
    UsageLimit,
    input_mark_for,
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
from .tool_selection import ToolAccess, ToolSelection

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


def _is_skill_grant(name: str) -> bool:
    """Skill or Skill(<name>). codex has no Skill tool: the grant opens nothing
    and closes nothing, so it must not decide whether the turn may write."""
    return name == SKILL_TOOL or (name.startswith(f"{SKILL_TOOL}(") and name.endswith(")"))


def _codex_reads_only(tools: ToolSelection) -> bool:
    """An allowlist whose non-skill entries are all read tools.

    always_skills puts Skill(<name>) on every turn, a non-owner's included.
    Counted as a write tool, it turned a read-only turn into workspace-write
    with network and --approve-for-me (W1).
    """
    # A turn with no tools at all reads nothing either; left to the profile
    # sandbox it ran workspace-write with network and --approve-for-me.
    if tools.access is ToolAccess.FORBIDDEN:
        return True
    return tools.access is ToolAccess.ALLOWLIST and all(
        is_read_only_tool(name) for name in tools.names if not _is_skill_grant(name)
    )


def _codex_can_act(tools: ToolSelection) -> bool:
    return tools.can_act and not _codex_reads_only(tools)


def _codex_mcp_config_args(
    mcp_servers: Mapping[str, McpServerSpec],
    *,
    can_act: bool = False,
    is_owner: bool = False,
    turn_context: Mapping[str, str] | None = None,
) -> list[str]:
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
            # env belongs to the process this engine spawns. A remote server
            # has none, so it goes in the stdio branch only -- claude and agy
            # have no place for it on a remote entry either, and outside the
            # branch the same profile ran differently per engine (sca-wdzh).
            for key, value in server.resolved_env().items():
                args += ["-c", f"{prefix}.env.{key}={CodexEngine._toml_string(value)}"]
            # Same stdio-only placement as env: a read-only turn omits the gate
            # var so the spawned server sees no write clearance (H6/H7/H8), and
            # the channel var is omitted when no channel token is in context.
            for key, value in server.turn_env_for(
                can_act=can_act, is_owner=is_owner, turn_context=turn_context
            ).items():
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
    #: 이번 턴에 부른 도구 이름. 진행 표시와 트러블슈팅이 쓴다.
    tools: list[str]
    #: 샌드박스가 쓰기를 막은 자리. 턴은 성공으로 끝나도 시킨 일은 못 했다.
    blocked: list[str]
    #: 샌드박스 밖 도구로 돌아간 자리. 막힌 것보다 무겁다.
    bypass: list[str]


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

    @classmethod
    def configured_capabilities(
        cls, spec: EngineSpec, mcp_servers: Mapping[str, McpServerSpec]
    ) -> EngineCapabilities:
        # MCP servers do not move this engine's axis: codex takes no tool
        # argument at all, so its restriction comes from the sandbox alone.
        return cls._capabilities_for_sandbox(
            str(spec.options.get("sandbox", "danger-full-access"))
        )

    @classmethod
    def _capabilities_for_sandbox(cls, sandbox: str) -> EngineCapabilities:
        isolation = _SANDBOX_ISOLATION.get(sandbox, ExecutionIsolation.NONE)
        return dataclasses.replace(
            cls.capabilities,
            execution_isolation=isolation,
            tool_restriction=(
                ToolRestriction.COARSE_SANDBOX
                if isolation is not ExecutionIsolation.NONE
                else ToolRestriction.NONE
            ),
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
        if _codex_reads_only(request.tools):
            return "read-only"
        return str(self.spec.options.get("sandbox", "danger-full-access"))

    def capabilities_for(self, request: EngineRequest) -> EngineCapabilities:
        return dataclasses.replace(
            self._capabilities_for_sandbox(self._sandbox_for(request)),
            # A resumed turn carries this turn's instructions inside the prompt
            # string, next to the Slack input. The CLI keeps the first turn's
            # developer_instructions whatever we pass (measured 2026-09-19), so
            # that layer is no longer where this turn's instructions live
            # (sca-ivs).
            instruction_boundary=(
                InstructionBoundary.PROMPT_ONLY if request.resume
                else InstructionBoundary.NATIVE
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
            adapter_added_bytes=base.adapter_added_bytes + utf8_bytes(input_mark_for(request)),
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
        # codex exec hardcodes approval_policy=never in non-interactive JSON
        # mode -- no -c override changes it, confirmed against codex-cli
        # 0.153.4 -- and under that policy every MCP-backed tool call fails
        # outright with "MCP tool call requires approval, but approval
        # policy is never" even though the server started fine and the
        # model picked the right tool (2026-09-30, DE에이전트 시험). --approve-for-me
        # routes approval through automatic review instead, which is the
        # only path found that lets MCP tools actually run headless. It
        # always runs under workspace-write, so it only substitutes when
        # that's the sandbox this turn already wanted; a read-only turn
        # keeps the stricter path and simply cannot call MCP tools, same as
        # before this fix.
        auto_approve = sandbox == "workspace-write"
        cmd: list[str] = [binary, "exec"]
        if auto_approve:
            cmd.append("--approve-for-me")
        if request.resume:
            cmd.append("resume")
        cmd += ["--json", "--skip-git-repo-check"]
        if self.spec.options.get("network") and sandbox == "workspace-write":
            # The key only lifts workspace-write; a read-only turn keeps it off
            # so the command shows the network closed, not just the sandbox.
            cmd += ["-c", "sandbox_workspace_write.network_access=true"]
        cmd += ["-m", request.require_model()]
        # Always sent, never omitted: leaving it out lets the CLI pick its own
        # default while the audit records the requested value (sca-3kzk).
        cmd += ["-c", f"model_reasoning_effort={self._toml_string(self.resolve_effort(request))}"]
        cmd += _codex_mcp_config_args(
            self.profile.mcp_servers,
            can_act=_codex_can_act(request.tools),
            is_owner=request.trust_level is TrustLevel.OWNER,
            turn_context=request.turn_context,
        )
        if not request.resume:
            instructions = request.system_prompt + self._session_path_note(request)
            if instructions:
                cmd += ["-c", f"developer_instructions={self._toml_string(instructions)}"]

        if request.resume:
            # resume doesn't accept --sandbox or -C; achieve the same effect via config keys instead.
            cmd += ["-c", f"sandbox_mode={self._toml_string(sandbox)}",
                   request.require_session_id(), "--", self._resume_prompt(request)]
        else:
            # -c sandbox_mode=, not the --sandbox flag: the flag form errors
            # with "cannot be used with '--approve-for-me'" while the config
            # override doesn't, and both restrict the turn the same way
            # (confirmed 2026-09-30).
            cmd += ["-c", f"sandbox_mode={self._toml_string(sandbox)}",
                    "-C", str(request.workdir), "--",
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
        return f"{note}{input_mark_for(request)}" if note else ""

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
            + input_mark_for(request)
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
        raw = {
            "thread_id": thread_id, "tool_errors": tool_errors, "usage": usage_data,
            "tools": stream.tools, "blocked": stream.blocked, "bypass": stream.bypass,
        }
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

    #: 모델 하나가 지금 요청을 못 받는 상태를 알리는 문구. 2026-10-19 sol
    #: 장애에서 모았다 - 15:57 은 "Selected model is at capacity", 23:46 은
    #: 재시도를 다 쓴 429 로 끝났다. 계정 벽과 달리 모델을 바꾸면 풀린다.
    MODEL_OUTAGE_HINTS: ClassVar[tuple[str, ...]] = (
        "selected model is at capacity",
        "429 too many requests",
    )
    #: 계정 단위 벽. 모델을 바꿔도 같으므로 모델 장애로 읽지 않는다.
    ACCOUNT_LIMIT_HINTS: ClassVar[tuple[str, ...]] = (
        "workspace is out of credits",
        "out of credits",
        "usage limit",
    )
    #: 출력에 이 표식이 있으면 그 턴은 이미 도구를 돌렸다. 다른 모델로 다시
    #: 돌리면 같은 쓰기가 두 번 난다 - 폴백하지 않는다.
    TOOL_EVENT_MARKS: ClassVar[tuple[str, ...]] = (
        'command_execution"',
        '"mcp_tool_call"',
        '"file_change"',
    )

    def detect_model_outage(self, stdout: str, stderr: str = "") -> bool:
        """Codex 실패가 모델 하나의 일시 장애인지 가른다 (B9). 계정 한도는 제외.

        계정 크레딧 소진·사용 한도가 먼저 걸리면 모델을 바꿔도 같으므로 False
        다 - 429 가 섞여 있어도 계정 벽이면 폴백하지 않는다.
        """
        text = f"{stdout or ''}\n{stderr or ''}".lower()
        if any(hint in text for hint in self.ACCOUNT_LIMIT_HINTS):
            return False
        return any(hint in text for hint in self.MODEL_OUTAGE_HINTS)

    def tools_already_ran(self, stdout: str) -> bool:
        return any(mark in (stdout or "") for mark in self.TOOL_EVENT_MARKS)

    #: 샌드박스가 쓰기를 막았을 때 출력에 나오는 문구 (2026-09-16 세션에서 수집,
    #: 마메치 codex_runner.SANDBOX_BLOCKED_MARKS). 이 문구가 있으면 모델은
    #: 시킨 일을 못 했는데 턴은 성공으로 끝나므로, 잡아서 남기지 않으면 실패가
    #: 성공으로 집계된다. codex CLI 공통 문구라 조직 값이 아니다.
    SANDBOX_BLOCKED_MARKS: ClassVar[tuple[str, ...]] = (
        "blocked by read-only sandbox",
        "patch rejected",
        "rejected by user approval settings",
        "Read-only file system",
    )
    #: 샌드박스 밖에서 파일을 쓸 수 있는 도구. 셸이 막히면 모델이 이쪽으로
    #: 돌아간다 — 샌드박스를 지나지 않아 무엇을 고쳤는지 기록에 안 남는다.
    SANDBOX_BYPASS_TOOLS: ClassVar[tuple[str, ...]] = (
        "browser.browser_run_code_unsafe",
    )

    @staticmethod
    def _item_text(item: Mapping[str, Any]) -> str:
        """아이템에서 차단 문구를 찾을 텍스트를 모은다. 명령 자체(command/
        parsed_cmd/arguments)는 뺀다 — 모델이 검색한 낱말이 그대로 걸려 오탐이
        되기 때문이다 (마메치 codex_runner._item_text)."""
        parts: list[str] = []
        for key, value in (item or {}).items():
            if key in ("command", "parsed_cmd", "arguments"):
                continue
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, (dict, list)):
                parts.append(json.dumps(value, ensure_ascii=False))
        return "\n".join(parts)

    @classmethod
    def _blocked_mark(cls, item: Mapping[str, Any]) -> str | None:
        """샌드박스 차단 문구가 있으면 그 문구를 돌려준다."""
        text = cls._item_text(item).lower()
        for mark in cls.SANDBOX_BLOCKED_MARKS:
            if mark.lower() in text:
                return mark
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

    @classmethod
    def _parse_jsonl(cls, output: str) -> _CodexStream:
        """Reads as far as the stream allows and says whether anything was
        unreadable, rather than raising. A run cut off mid-write leaves a
        truncated last line, and that is exactly when the events before it
        are worth keeping."""
        thread_id: str | None = None
        text = ""
        usage: dict[str, Any] = {}
        tool_errors: list[str] = []
        tools: list[str] = []
        blocked: list[str] = []
        bypass: list[str] = []
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
                item_type = item.get("type")
                if item_type == "agent_message" and isinstance(item.get("text"), str):
                    text = item["text"]
                elif item_type == "mcp_tool_call":
                    label = f"{item.get('server', '?')}.{item.get('tool', '?')}"
                    tools.append(label)
                    if item.get("status") == "failed":
                        error = item.get("error") or {}
                        message = error.get("message") if isinstance(error, Mapping) else None
                        tool_errors.append(f"{label} : {message or '실패'}")
                    if label in cls.SANDBOX_BYPASS_TOOLS and item.get("status") != "failed":
                        bypass.append(label)
                    mark = cls._blocked_mark(item)
                    if mark:
                        blocked.append(f"{label} : {mark}")
                elif item_type in ("command_execution", "async_command_execution"):
                    tools.append("shell")
                    mark = cls._blocked_mark(item)
                    if mark:
                        blocked.append(f"shell : {mark}")
                elif item_type == "error" and isinstance(item.get("message"), str):
                    tool_errors.append(item["message"])
            elif kind == "turn.completed":
                event_usage = event.get("usage")
                if isinstance(event_usage, dict):
                    usage = event_usage
        return _CodexStream(
            thread_id, text, usage, tool_errors, malformed, tools, blocked, bypass,
        )

    @staticmethod
    def _toml_string(value: str) -> str:
        """``-c key=value``'s value parses as TOML; JSON string escaping
        matches TOML's basic string escaping, so it's safe to reuse."""
        return json.dumps(value, ensure_ascii=False)
