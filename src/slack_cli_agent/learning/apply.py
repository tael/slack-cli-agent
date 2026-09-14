"""학습 제안을 지식 파일에 반영/되돌리기.

원본 learn.py 의 apply_items()/apply_proposal()/revert() 를 대응한다. 각 줄
끝에 꼬리표(``<!-- learn:YYYY-MM-DD -->``)를 달아 되돌릴 항목을 찾는다.

원본은 파일 전체를 ``write_text()`` 로 통짜 덮어썼다. 쓰는 도중 다른 프로세스가
읽으면 잘린 파일이 읽힌다. 여기서는 임시 파일에 쓰고 ``os.replace`` 로
교체한다.
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
    """제안 항목을 지식 파일에 쌓는다. 이미 있는 문장은 다시 넣지 않는다."""

    def __init__(self, knowledge_dir: Path, bot_name: str) -> None:
        self._dir = knowledge_dir
        self._bot_name = bot_name

    def apply(self, proposal: LearningProposal) -> dict[str, int]:
        """제안을 반영하고, 무엇을 몇 건 넣었는지 파일 이름별로 돌려준다."""
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
    """그날 자동으로 넣은 줄만 지운다."""

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
