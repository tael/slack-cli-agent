"""Antigravity CLI(agy) adapter for the Gemini engine.

Findings this module relies on are in docs/agy-실측.md. Key differences
from Claude/Codex:

- ``-p`` must be the last flag: anything after it on the command line is
  swallowed as the prompt text, so ``-p <prompt>`` has to be the tail.
- Output is JSONL (``--output-format stream-json``), and the payload the
  ``json`` format would have printed arrives as the last ``result`` event.
  parse() still reads the single-object form, so a CLI version without
  stream-json keeps working (sca-8ks). Exit code 0 doesn't mean success —
  a headless permission denial also exits 0, so ``status`` is checked too.
- Model name and ``--effort`` are mutually exclusive when the model name
  already carries an effort suffix (e.g. ``gemini-3.8-flash-medium``).
  This engine always sends the suffix-free name plus ``--effort``.
- There's no dedicated system-prompt flag; it's prefixed onto the prompt
  text, same spot readable_paths_note() lands for Codex.

MCP servers (sca-kos.2): `agy --help` lists every top-level flag and none
of them takes inline MCP config — the only documented path is a
workspace-local file, <work_root>/.agents/mcp_config.json (docs/agy-실측.md
section 4). This engine writes that file the same defensive way
_ensure_settings_file() already does: read what's there, replace only the
mcpServers key, write back, so it never wipes something added another way.
Schema (mcpServers.<name>.command/args/env/cwd for stdio,
serverUrl/headers for remote, disabled, disabledTools) is per
antigravity.google/docs/cli/mcp. Because the server data lands in a file
rather than argv, TestMcpInjectionIsolation can't observe it through
build_command() and stays xfail for this engine (see that test's
docstring).

Skills (sca-kos.3): docs/agy-실측.md already established
<work_root>/.agents/skills/<name>/ as the workspace-local convention —
same limitation as MCP, no flag takes an arbitrary directory, so
TestSkillDirectoryIsolation also stays xfail here.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar

from ..config.profile import McpServerSpec
from .base import (
    UNTRUSTED_INPUT_MARK,
    ElapsedSource,
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

_VALID_EFFORTS = frozenset({"low", "medium", "high"})
_MODEL_EFFORT_SUFFIXES = ("high", "medium", "low")

_SETTINGS_CONTENT: Mapping[str, Any] = {
    "allowNonWorkspaceAccess": True,
    "permissions": {"allow": ["*"], "deny": [], "ask": []},
}

# sca-dyb.4 — agy has no cache-write-equivalent metric at all (confirmed
# 2026-09-16 against the real CLI), so cache_creation_tokens has no entry
# here and is always unavailable for this engine.
_USAGE_KEY_MAP: Mapping[str, str] = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_tokens": "cache_read_tokens",
}


def _agy_mcp_servers(mcp_servers: Mapping[str, McpServerSpec]) -> dict[str, Any]:
    servers: dict[str, Any] = {}
    for name, server in mcp_servers.items():
        entry: dict[str, Any]
        if server.is_remote:
            entry = {"serverUrl": server.url}
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
        if server.disabled:
            entry["disabled"] = True
        if server.disabled_tools:
            entry["disabledTools"] = list(server.disabled_tools)
        servers[name] = entry
    return servers


def _write_if_changed(path: Path, content: Mapping[str, Any]) -> None:
    """Leaves the file alone when nothing would change. A learning batch is
    read-only in operational terms, and two runs sharing a home would otherwise
    keep rewriting the same file (sca-dyb.13).
    """
    rendered = json.dumps(content, ensure_ascii=False, indent=2)
    try:
        if path.read_text(encoding="utf-8") == rendered:
            return
    except OSError:
        pass
    path.write_text(rendered, encoding="utf-8")


def _normalize_effort(value: str) -> str:
    if not value:
        return "medium"
    if value in _VALID_EFFORTS:
        return value
    # Unrecognized values, including xhigh/max, fall to the highest valid
    # tier rather than silently downgrading to medium.
    return "high"


def _split_model_suffix(model: str) -> tuple[str, str | None]:
    for suffix in _MODEL_EFFORT_SUFFIXES:
        marker = f"-{suffix}"
        if model.endswith(marker):
            return model[: -len(marker)], suffix
    return model, None




class GeminiEngine(Engine):
    name = "gemini"
    shell_always_attached = True

    # --dangerously-skip-permissions is always passed (headless mode auto-denies
    # otherwise), so nothing is restricted. system_prompt and prompt go into one
    # -p string; UNTRUSTED_INPUT_MARK labels the split but nothing enforces it.
    capabilities = EngineCapabilities(
        tool_restriction=ToolRestriction.NONE,
        execution_isolation=ExecutionIsolation.NONE,
        instruction_boundary=InstructionBoundary.PROMPT_ONLY,
    )

    def prepare(self, request: EngineRequest) -> None:
        self._ensure_settings_file(self.spec.home_dir)
        self._ensure_keychain_link(self.spec.home_dir)
        self._ensure_mcp_config_file(request.workdir, self.profile.mcp_servers)

    def footprint_for(self, request: EngineRequest) -> PayloadFootprint:
        """agy takes one prompt argument, so instructions always ride in it.
        On a resumed turn those bytes join the session context (sca-ygd)."""
        base = super().footprint_for(request)
        return dataclasses.replace(
            base,
            adapter_added_bytes=base.adapter_added_bytes + utf8_bytes(UNTRUSTED_INPUT_MARK),
            instruction_transport=INSTRUCTION_TRANSPORT_USER_PROMPT,
            instruction_replayed_on_resume=request.resume,
        )

    #: How long agy keeps running past --print-timeout before it exits.
    #: Measured 2026-09-20: a 5s limit ended at 13.2s and a 15s limit at 19.7s,
    #: 8.2s and 4.7s past. It writes the partial answer and a result event in
    #: that stretch, so the runner's hard kill has to come after it -- told the
    #: same figure, the runner always wins and that output is lost (sca-ocie).
    PRINT_TIMEOUT_MARGIN_SEC: ClassVar[float] = 15.0

    def build_command(self, request: EngineRequest) -> list[str]:
        spec = self.spec

        base_model, suffix_effort = _split_model_suffix(request.require_model())
        effort = _normalize_effort(request.effort or suffix_effort or "")
        limit = (
            request.timeout_sec if request.timeout_sec is not None else self.settings.request_timeout_sec
        )
        # Floor, never round up: the CLI has to finish inside the runner's limit.
        # Under a limit shorter than the margin this is best effort -- 1s is
        # the floor because 0s means "no limit" to agy, the opposite of what
        # is meant, and the runner will still get there first (codex review).
        timeout_sec = max(1, int(limit - self.PRINT_TIMEOUT_MARGIN_SEC))

        cmd: list[str] = [
            str(spec.binary),
            "--output-format", "stream-json",
            "--dangerously-skip-permissions",
            "--model", base_model,
            "--effort", effort,
        ]
        for path in request.readable_dirs:
            cmd += ["--add-dir", str(path)]
        if request.resume:
            cmd += ["--conversation", request.require_session_id()]
        cmd += ["--print-timeout", f"{timeout_sec}s"]

        prompt = (
            request.system_prompt
            + self.readable_paths_note(request.readable_dirs)
            + self.write_paths_note(request)
            + self.tool_ban_note(request)
            + UNTRUSTED_INPUT_MARK
            + request.prompt
        )
        cmd += ["-p", prompt]
        return cmd

    # 2026-09-19 실측 — step_update 의 step_type 이 "tool" 인 이벤트에만
    # tool_name 이 온다. 나머지 step_type(user_input, agent_response)은
    # 도구 호출이 아니다.
    streams_progress = True

    def progress_tool_name(self, line: str) -> str:
        event = json_object_line(line)
        if event is None or event.get("event") != "step_update":
            return ""
        step = event.get("step_update")
        if not isinstance(step, Mapping):
            return ""
        kind = step.get("step_type")
        # The answer arrives as text_delta fragments, so this name repeats
        # dozens of times per answer. ProgressLogReader collapses consecutive
        # repeats, which is what keeps it to one line on screen (sca-92g).
        if kind == "agent_response":
            return "agent_response"
        if kind != "tool":
            return ""
        name = step.get("tool_name")
        return name if isinstance(name, str) else ""

    def new_session_id(self) -> str:
        # agy mints the real conversation_id on first run; this is only a
        # placeholder until session_id_from() replaces it.
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

    #: 2026-09-19 실측 (agy 바이너리) - 로그인이 풀리면 "Error: authentication
    #: required. Run '<명령>' to log in." 을 내고, 로그인 상태 화면은 "You are
    #: currently not signed in." 을 낸다. 인증 실패는 JSON 이 아니라 이 줄로
    #: 나와 bad_json 으로 떨어지므로 실패 종류로는 못 가린다.
    #: 문구는 CLI 가 내는 문장 형태 그대로 잡는다. 'authentication required'
    #: 조각만 보면 MCP 서버가 낸 같은 말에도 엔진이 바뀐다.
    AUTH_FAILURE_MARKERS: ClassVar[tuple[str, ...]] = (
        "authentication required. Run",
        "You are currently not signed in",
    )
    AUTH_FAILURE_NOTE = "gemini 로그인이 풀렸다. agy 를 실행해 다시 로그인해야 한다."

    def parse(self, stdout: str, stderr: str, returncode: int) -> EngineResponse:
        payload = self._payload_from(stdout)
        if payload is None:
            return EngineResponse(
                ok=False, body="Gemini 응답 형식을 읽지 못했습니다.", session_id=None,
                model_actual=None, elapsed=0.0, turns=None, usage=None,
                raw={"stdout": stdout, "stderr": stderr, "returncode": returncode},
                failure_reason="bad_json",
                failure_detail=FailureDetail(exit_code=returncode, stdout_chars=len(stdout)),
            )

        session_id = payload.get("conversation_id") or None
        elapsed, elapsed_source = self._elapsed_from_payload(payload)
        turns = payload.get("num_turns")
        usage = Usage.from_native(payload.get("usage"), _USAGE_KEY_MAP)
        raw = dict(payload)
        status = str(payload.get("status") or "")

        if status != "SUCCESS":
            body = str(payload.get("error") or "").strip() or "Gemini 실행에 실패했습니다."
            return EngineResponse(
                ok=False, body=body, session_id=session_id, model_actual=None,
                elapsed=elapsed, turns=turns, usage=usage, raw=raw, failure_reason="is_error",
                failure_detail=FailureDetail(code=status), elapsed_source=elapsed_source,
            )

        # 2026-09-17 실측 — agy 는 실패에 exit 1 과 status=ERROR 를 함께 낸다.
        # status 만 보던 탓에 여기만 종료코드를 실패 판정에 안 썼다(sca-e9a).
        # 조건을 더하는 것이지 status 를 대체하는 것이 아니다: 권한 거부는
        # exit 0 으로 나므로 위의 status 판정이 그대로 그 경로를 잡는다.
        if returncode != 0:
            return EngineResponse(
                ok=False, body="처리에 실패했습니다.", session_id=session_id, model_actual=None,
                elapsed=elapsed, turns=turns, usage=usage, raw=raw,
                failure_reason="nonzero_exit",
                failure_detail=FailureDetail(exit_code=returncode, stdout_chars=len(stdout)),
                elapsed_source=elapsed_source,
            )

        body = str(payload.get("response") or "").strip()
        if not body:
            return EngineResponse(
                ok=False, body="응답이 비어 있습니다.", session_id=session_id, model_actual=None,
                elapsed=elapsed, turns=turns, usage=usage, raw=raw, failure_reason="empty_response",
                failure_detail=FailureDetail(
                    exit_code=returncode, stdout_chars=len(stdout), code=status,
                ),
                elapsed_source=elapsed_source,
            )
        return EngineResponse(
            ok=True, body=body, session_id=session_id, model_actual=None, elapsed=elapsed,
            turns=turns, usage=usage, raw=raw, failure_reason=None, elapsed_source=elapsed_source,
        )

    @staticmethod
    def _payload_from(stdout: str) -> Mapping[str, Any] | None:
        """The result object, from either output format.

        stream-json wraps it in the last ``{"event": "result"}`` line -- last
        rather than first because a resumed run can emit more than one. A
        single object with no "event" key is the old --output-format json
        shape, kept as a fallback for a CLI version that drops stream-json
        (sca-8ks). A stream that never reached a result event is a cut-off
        run, not an empty answer, so it stays unreadable.
        """
        found: Mapping[str, Any] | None = None
        for line in stdout.splitlines():
            event = json_object_line(line)
            if event is None or event.get("event") != "result":
                continue
            result = event.get("result")
            if isinstance(result, Mapping):
                found = result
        if found is not None:
            return found
        whole = json_object_line(stdout)
        if whole is None or "event" in whole:
            return None
        return whole

    @staticmethod
    def _elapsed_from_payload(payload: Mapping[str, Any]) -> tuple[float, ElapsedSource]:
        """duration_seconds confirmed 2026-09-16 against the real CLI — agy has
        always filled this field, unlike claude/codex."""
        duration = payload.get("duration_seconds")
        if isinstance(duration, (int, float)):
            return float(duration), ElapsedSource.ENGINE
        return 0.0, ElapsedSource.UNKNOWN

    def detect_usage_limit(self, response: EngineResponse) -> UsageLimit | None:
        # No observed weekly/subscription limit concept for agy yet
        # (docs/agy-실측.md has no evidence either way) — return None
        # until a real limit response is captured.
        return None

    @staticmethod
    def _ensure_keychain_link(home_dir: Path | None) -> None:
        """The CLI caches its token in the macOS login keychain. With HOME
        moved per bot, Security finds no default keychain and macOS shows a
        modal asking where to store 'antigravity' on every token refresh."""
        if home_dir is None:
            return
        real = Path.home() / "Library" / "Keychains"
        link = home_dir / "Library" / "Keychains"
        if not real.is_dir() or link.resolve() == real.resolve():
            return
        if link.exists() and not link.is_symlink():
            return
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            link.unlink()
        link.symlink_to(real)

    @staticmethod
    def _ensure_settings_file(home_dir: Path | None) -> None:
        if home_dir is None:
            return
        settings_path = home_dir / ".gemini" / "antigravity-cli" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        # agy keeps unrelated values here (colorScheme, editor,
        # modelProvider). Overwriting the file would drop them, so only
        # our keys are replaced. An unreadable file is treated as empty.
        existing: dict[str, Any] = {}
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, Mapping):
            existing = dict(loaded)
        existing.update(_SETTINGS_CONTENT)
        _write_if_changed(settings_path, existing)

    @staticmethod
    def _ensure_mcp_config_file(workdir: Path, mcp_servers: Mapping[str, McpServerSpec]) -> None:
        servers = _agy_mcp_servers(mcp_servers)
        if not servers:
            # An installed bot with no configured servers must not create
            # this file at all — see docs/패키징-경계.md.
            return
        config_path = workdir / ".agents" / "mcp_config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict[str, Any] = {}
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, Mapping):
            existing = dict(loaded)
        existing["mcpServers"] = servers
        _write_if_changed(config_path, existing)
