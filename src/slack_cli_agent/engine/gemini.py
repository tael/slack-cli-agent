"""Antigravity CLI(agy) adapter for the Gemini engine.

Findings this module relies on are in docs/agy-실측.md. Key differences
from Claude/Codex:

- ``-p`` must be the last flag: anything after it on the command line is
  swallowed as the prompt text, so ``-p <prompt>`` has to be the tail.
- Output is one JSON object (not an event array like Claude, not JSONL
  like Codex). Exit code 0 doesn't mean success — a headless permission
  denial also exits 0, so ``status`` is the only reliable signal.
- Model name and ``--effort`` are mutually exclusive when the model name
  already carries an effort suffix (e.g. ``gemini-3.8-flash-medium``).
  This engine always sends the suffix-free name plus ``--effort``.
- There's no dedicated system-prompt flag; it's prefixed onto the prompt
  text, same spot readable_paths_note() lands for Codex.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .base import Engine, EngineRequest, EngineResponse, Usage, UsageLimit

_VALID_EFFORTS = frozenset({"low", "medium", "high"})
_MODEL_EFFORT_SUFFIXES = ("high", "medium", "low")

_SETTINGS_CONTENT: Mapping[str, Any] = {
    "allowNonWorkspaceAccess": True,
    "permissions": {"allow": ["*"], "deny": [], "ask": []},
}


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

    def prepare(self, request: EngineRequest) -> None:
        self._ensure_settings_file(self.spec.home_dir)

    def build_command(self, request: EngineRequest) -> list[str]:
        spec = self.spec

        base_model, suffix_effort = _split_model_suffix(request.model)
        effort = _normalize_effort(request.effort or suffix_effort or "")
        timeout_sec = int(self.settings.request_timeout_sec)

        cmd: list[str] = [
            str(spec.binary),
            "--output-format", "json",
            "--dangerously-skip-permissions",
            "--model", base_model,
            "--effort", effort,
        ]
        for path in request.readable_dirs:
            cmd += ["--add-dir", str(path)]
        if request.resume:
            cmd += ["--conversation", request.session_id]
        cmd += ["--print-timeout", f"{timeout_sec}s"]

        prompt = (
            request.system_prompt
            + self.readable_paths_note(request.readable_dirs)
            + "\n\n"
            + request.prompt
        )
        cmd += ["-p", prompt]
        return cmd

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

    def parse(self, stdout: str, stderr: str, returncode: int) -> EngineResponse:
        try:
            payload = json.loads(stdout)
        except (json.JSONDecodeError, TypeError):
            return EngineResponse(
                ok=False, body="Gemini 응답 형식을 읽지 못했습니다.", session_id=None,
                model_actual=None, elapsed=0.0, turns=None, usage=None,
                raw={"stdout": stdout, "stderr": stderr, "returncode": returncode},
                failure_reason="bad_json",
            )
        if not isinstance(payload, Mapping):
            return EngineResponse(
                ok=False, body="Gemini 응답 형식을 읽지 못했습니다.", session_id=None,
                model_actual=None, elapsed=0.0, turns=None, usage=None,
                raw={"stdout": stdout, "stderr": stderr, "returncode": returncode},
                failure_reason="bad_json",
            )

        session_id = payload.get("conversation_id") or None
        elapsed = float(payload.get("duration_seconds") or 0.0)
        turns = payload.get("num_turns")
        usage = self._usage_from_native(payload.get("usage"))
        raw = dict(payload)
        status = str(payload.get("status") or "")

        if status != "SUCCESS":
            body = str(payload.get("error") or "").strip() or "Gemini 실행에 실패했습니다."
            return EngineResponse(
                ok=False, body=body, session_id=session_id, model_actual=None,
                elapsed=elapsed, turns=turns, usage=usage, raw=raw, failure_reason="is_error",
            )

        body = str(payload.get("response") or "").strip()
        if not body:
            return EngineResponse(
                ok=False, body="응답이 비어 있습니다.", session_id=session_id, model_actual=None,
                elapsed=elapsed, turns=turns, usage=usage, raw=raw, failure_reason="empty_response",
            )
        return EngineResponse(
            ok=True, body=body, session_id=session_id, model_actual=None, elapsed=elapsed,
            turns=turns, usage=usage, raw=raw, failure_reason=None,
        )

    def detect_usage_limit(self, response: EngineResponse) -> UsageLimit | None:
        # No observed weekly/subscription limit concept for agy yet
        # (docs/agy-실측.md has no evidence either way) — return None
        # until a real limit response is captured.
        return None

    @staticmethod
    def _usage_from_native(native: Any) -> Usage:
        if not isinstance(native, Mapping):
            return Usage()
        translated = {
            "input_tokens": native.get("input_tokens"),
            "output_tokens": native.get("output_tokens"),
            # agy already uses the common-vocabulary key for this one;
            # thinking_tokens has no counterpart and stays in raw only.
            "cache_read_input_tokens": native.get("cache_read_tokens"),
        }
        return Usage.from_mapping(translated)

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
        settings_path.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8",
        )
