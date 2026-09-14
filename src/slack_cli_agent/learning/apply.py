"""Applies and reverts learning proposals against knowledge files.

Each added line carries an `<!-- learn:YYYY-MM-DD -->` tag so a revert can
find exactly what a given day added. Writes go through a temp file + os.replace
so a concurrent reader never sees a half-written file.
"""

from __future__ import annotations

import os
from pathlib import Path

from .proposal import LearningProposal

STAMP_TEMPLATE = "<!-- learn:{day} -->"
SECTION_HEADING = "## 대화에서 배운 것"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class LearningApplier:
    """Appends proposal items to knowledge files, skipping duplicates."""

    def __init__(self, knowledge_dir: Path, bot_name: str) -> None:
        self._dir = knowledge_dir
        self._bot_name = bot_name

    def apply(self, proposal: LearningProposal) -> dict[str, int]:
        """Applies the proposal and returns the count added per file."""
        done: dict[str, int] = {}
        for name, items in proposal.channel_knowledge.items():
            n = self._apply_items(self._channel_file(name), proposal.day, items, name)
            if n:
                done[name] = n
        n = self._apply_items(
            self._dir / "_writing-style.md", proposal.day, proposal.writing_style, "",
        )
        if n:
            done["_writing-style"] = n
        n = self._apply_items(
            self._dir / "_corrections.md", proposal.day, proposal.corrections, "",
        )
        if n:
            done["_corrections"] = n
        return done

    def _channel_file(self, name: str) -> Path:
        return self._dir / f"{name}.md"

    def _apply_items(
        self, path: Path, day: str, items: tuple[str, ...], heading: str,
    ) -> int:
        if not items:
            return 0
        text = path.read_text(encoding="utf-8") if path.exists() else f"# {path.stem}\n"
        if SECTION_HEADING not in text:
            text += (
                f"\n\n{SECTION_HEADING}\n\n"
                f"{self._bot_name}가 대화에서 확인한 것을 날짜와 함께 쌓는다.\n"
                "틀린 것이 보이면 그 줄을 지우면 된다.\n"
            )
        stamp = STAMP_TEMPLATE.format(day=day)
        lines = []
        for it in items:
            it = it.strip()
            if not it or it in text:
                continue
            lines.append(f"- {it} {stamp}")
        if not lines:
            return 0
        if heading and f"### {heading}" not in text:
            text += f"\n### {heading}\n"
        text += "\n".join(lines) + "\n"
        _atomic_write(path, text)
        return len(lines)


class LearningReverter:
    """Removes only the lines a given day's batch added."""

    def __init__(self, knowledge_dir: Path) -> None:
        self._dir = knowledge_dir

    def revert(self, day: str) -> int:
        if not self._dir.is_dir():
            return 0
        stamp = STAMP_TEMPLATE.format(day=day)
        removed = 0
        for path in self._dir.glob("*.md"):
            text = path.read_text(encoding="utf-8")
            if stamp not in text:
                continue
            lines = text.splitlines()
            keep = [line for line in lines if stamp not in line]
            removed += len(lines) - len(keep)
            _atomic_write(path, "\n".join(keep) + "\n")
        return removed
