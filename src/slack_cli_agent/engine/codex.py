"""Codex CLI 어댑터.

Claude 와 세 가지가 다르다.

- 시스템 지침 : Codex 는 ``developer_instructions`` 를 스레드를 열 때만
  받는다. 재개 턴에 새 값을 줘도 최초 값이 이긴다(2026-09-11 확인). 그래서
  최초 턴에만 시스템 프롬프트를 싣고, directives_for_turn() 이 턴마다
  바뀌는 것만 본문 앞에 붙이도록 남겨 둔다.
- 세션 : Codex CLI 가 thread_id 를 발급한다. session_id_from() 이 그것을
  돌려준다. new_session_id() 가 만드는 값은 그 전까지 쓰는 자리 표시자일
  뿐이다.
- 경로 : ``--add-dir`` 에 해당하는 인자가 없다. readable_paths_note() 로
  문장을 만들어 시스템 지침에 붙인다.
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
            # 읽기 전용 샌드박스는 통신도 막는다. 여는 것은 프로필이 정한다.
            cmd += ["-c", "sandbox_workspace_write.network_access=true"]
        if request.model:
            cmd += ["-m", request.model]
        if request.effort:
            cmd += ["-c", f"model_reasoning_effort={self._toml_string(request.effort)}"]
        if not request.resume and request.system_prompt:
            cmd += ["-c", f"developer_instructions={self._toml_string(request.system_prompt)}"]

        if request.resume:
            # resume 에는 --sandbox 와 -C 인자가 없다. 설정값으로 같은 효과를 낸다.
            cmd += ["-c", f"sandbox_mode={self._toml_string(sandbox)}",
                   request.session_id, request.prompt]
        else:
            cmd += ["--sandbox", sandbox, "-C", str(request.workdir), request.prompt]
        return cmd

    def new_session_id(self) -> str:
        # 실제 스레드 ID 는 CLI 가 첫 실행에서 발급한다. 이 값은 그 전까지 쓰는
        # 자리 표시자일 뿐이라 재개 시점에는 session_id_from() 이 돌려준 실제
        # 값으로 갈아 끼워야 한다.
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
        # Codex 는 구독 주간 한도라는 개념이 없다. 원본에도 이 판정이 없다.
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
        """``-c key=value`` 의 value 는 TOML 로 해석된다. JSON 문자열 표기가
        TOML 기본 문자열과 같은 이스케이프를 쓰므로 그대로 쓴다."""
        return json.dumps(value, ensure_ascii=False)
