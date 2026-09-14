"""프롬프트 파일 로드.

원본 prompt_text 와 같은 실패 정책을 쓴다 — 파일이 없거나 비면 예외를 낸다.
가드 문구가 빠진 채로 답하는 것을 사고로 본다. 기본값으로 넘어가지 않는다.

프롬프트 파일은 요청마다 다시 읽는다. 재기동 없이 반영되어야 하기 때문이다.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from pathlib import Path

from ..core.errors import MissingPromptError


class PromptLibrary:
    """`<이름>.md` 파일을 읽고 자리표를 채운다."""

    def __init__(self, prompts_dir: Path, placeholders: Mapping[str, str] | None = None) -> None:
        self._dir = prompts_dir
        self._placeholders = dict(placeholders or {})

    def text(self, name: str, keep_slots: Collection[str] = ()) -> str:
        """파일이 없거나 비면 MissingPromptError. 가드가 빠진 채 답하지 않는다."""
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
