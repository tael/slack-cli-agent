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
- Paths: there's no --add-dir equivalent. readable_paths_note() builds
  a sentence appended to the system instructions instead.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .base import Engine, EngineRequest, EngineResponse, Usage, UsageLimit


class CodexEngine(Engine):
    name = "codex"

    def build_command(self, request: EngineRequest) -> list[str]:
        binary = str(self.spec.binary)
        sandbox = str(self.spec.options.get("sandbox", "read-only"))
        cmd: list[str] = [binary, "exec"]
        if request.resume:
            cmd.append("resume")
        cmd += ["--json", "--skip-git-repo-check"]
        if self.spec.options.get("network"):
            # The read-only sandbox blocks network too; the profile decides whether to open it.
            cmd += ["-c", "sandbox_workspace_write.network_access=true"]
        if request.model:
            cmd += ["-m", request.model]
        if request.effort:
            cmd += ["-c", f"model_reasoning_effort={self._toml_string(request.effort)}"]
        if not request.resume and request.system_prompt:
            instructions = request.system_prompt + self.readable_paths_note(request.readable_dirs)
            cmd += ["-c", f"developer_instructions={self._toml_string(instructions)}"]

        if request.resume:
            # resume doesn't accept --sandbox or -C; achieve the same effect via config keys instead.
            cmd += ["-c", f"sandbox_mode={self._toml_string(sandbox)}",
                   request.session_id, request.prompt]
        else:
            cmd += ["--sandbox", sandbox, "-C", str(request.workdir), request.prompt]
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
                failure_reason="nonzero_exit",
            )
        try:
            thread_id, text, usage_data, tool_errors = self._parse_jsonl(stdout)
        except (json.JSONDecodeError, ValueError, TypeError):
            return EngineResponse(
                ok=False, body="Codex 응답 형식을 읽지 못했습니다.", session_id=None,
                model_actual=None, elapsed=0.0, turns=None, usage=None,
                raw={"stdout": stdout}, failure_reason="bad_json",
            )

        body = (text or "").strip()
        raw = {"thread_id": thread_id, "tool_errors": tool_errors}
        if not body:
            return EngineResponse(
                ok=False, body="Codex 응답이 비어 있습니다.", session_id=thread_id,
                model_actual=None, elapsed=0.0, turns=None,
                usage=Usage.from_mapping(usage_data), raw=raw,
                failure_reason="empty_response",
            )
        return EngineResponse(
            ok=True, body=body, session_id=thread_id, model_actual=None, elapsed=0.0,
            turns=None, usage=Usage.from_mapping(usage_data), raw=raw, failure_reason=None,
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
