# A missing or empty prompt file raises rather than falling back to a
# default -- answering without its guard text is treated as a failure, not
# a degraded mode. Files are reread on every request so edits take effect
# without a restart.

from __future__ import annotations

from collections.abc import Collection, Mapping
from pathlib import Path

from ..core.errors import MissingPromptError


class PromptLibrary:
    def __init__(self, prompts_dir: Path, placeholders: Mapping[str, str] | None = None) -> None:
        self._dir = prompts_dir
        self._placeholders = dict(placeholders or {})

    def text(self, name: str, keep_slots: Collection[str] = ()) -> str:
        path = self._dir / f"{name.lower()}.md"
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise MissingPromptError(f"프롬프트 파일을 읽지 못했다: {path}") from exc
        if not raw.strip():
            raise MissingPromptError(f"프롬프트 파일이 비어 있다: {path}")
        text = raw
        for key, value in self._placeholders.items():
            if key in keep_slots:
                continue
            text = text.replace(f"<<{key}>>", value)
        return text
