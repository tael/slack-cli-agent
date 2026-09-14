"""프롬프트·지식 파일 읽기와 쓰기.

이름은 브라우저에서 온다 -- root 밖을 가리키는 이름은 전부 거부한다.
"""

from __future__ import annotations

import os
from pathlib import Path


class PathEscapeError(Exception):
    pass


class FileEditor:
    def __init__(self, root: Path, *, suffix: str = ".md") -> None:
        self._root = root
        self._suffix = suffix

    def names(self) -> list[str]:
        if not self._root.is_dir():
            return []
        return sorted(p.stem for p in self._root.glob(f"*{self._suffix}") if p.is_file())

    def read(self, name: str) -> str:
        path = self._resolve(name)
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PathEscapeError(f"파일을 읽지 못했다: {name}") from exc

    def write(self, name: str, text: str) -> None:
        if not text.strip():
            raise ValueError(f"빈 내용은 저장할 수 없다: {name}")
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._resolve(name)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    def _resolve(self, name: str) -> Path:
        if "/" in name or "\\" in name or name in {"", ".", ".."}:
            raise PathEscapeError(f"허용되지 않는 이름이다: {name}")
        root_resolved = self._root.resolve()
        path = self._root / f"{name}{self._suffix}"
        resolved = path.resolve()
        try:
            resolved.relative_to(root_resolved)
        except ValueError as exc:
            raise PathEscapeError(f"root 밖을 가리키는 이름이다: {name}") from exc
        return path
