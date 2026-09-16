# Files are reread on every request so edits take effect without a restart.

from __future__ import annotations

from pathlib import Path

# A knowledge file whose first line starts with this is loaded only if the
# request text contains one of the trigger words listed after it; files
# without this marker are always loaded.
WHEN_MARK = "<!-- when:"

_SKIPPED_HEADER = (
    "# 지금 싣지 않은 주제별 지식\n\n"
    "아래 파일은 이번 요청 낱말과 맞지 않아 싣지 않았다.\n"
    "그 주제를 다루게 되면 파일을 직접 읽는다. 없는 것으로 답하지 않는다.\n\n"
)
_CHANNEL_HEADER = "# 이 채널의 지식\n\n앞에 적힌 것과 다르게 적힌 항목이 있으면 여기를 따른다.\n\n"
_LEARNED_HEADER = (
    "# 학습이 쌓은 지식\n\n"
    "대화에서 확인한 것을 날짜와 함께 쌓은 것이다. 사람이 쓴 지식과 어긋나면\n"
    "사람이 쓴 쪽을 따른다.\n\n"
)


class KnowledgeLoader:
    def __init__(
        self,
        persona_file: Path,
        knowledge_dir: Path,
        domain_file: Path | None = None,
        learned_dir: Path | None = None,
    ) -> None:
        self._persona_file = persona_file
        self._knowledge_dir = knowledge_dir
        self._domain_file = domain_file
        # None means this bot has no separate learned directory, which is the
        # shape every caller had before the two sources were split.
        self._learned_dir = learned_dir

    def persona_text(self, include_domain: bool = False) -> str:
        parts: list[str] = []
        if self._persona_file.exists():
            parts.append(self._persona_file.read_text(encoding="utf-8"))
        if include_domain and self._domain_file and self._domain_file.exists():
            parts.append(self._domain_file.read_text(encoding="utf-8"))
        return "\n\n".join(parts).strip()

    def knowledge_text(self, channel_slug: str, prompt: str = "") -> str:
        parts: list[str] = []
        skipped: list[Path] = []
        parts += self._from_dir(self._knowledge_dir, channel_slug, prompt, skipped)
        learned = (
            self._from_dir(self._learned_dir, channel_slug, prompt, skipped)
            if self._learned_dir is not None
            else []
        )
        if learned:
            parts.append(_LEARNED_HEADER + "\n\n".join(learned))
        if skipped:
            parts.append(
                _SKIPPED_HEADER + "\n".join(f"- {p.stem.lstrip('_')} : {p}" for p in skipped)
            )
        return "\n\n".join(parts).strip()

    def _from_dir(
        self, directory: Path, channel_slug: str, prompt: str, skipped: list[Path],
    ) -> list[str]:
        parts: list[str] = []
        low = (prompt or "").lower()
        if directory.is_dir():
            for common in sorted(directory.glob("_*.md")):
                text = common.read_text(encoding="utf-8")
                triggers = self._triggers(text)
                if triggers is not None and not any(t in low for t in triggers):
                    skipped.append(common)
                    continue
                parts.append(text)
        if channel_slug:
            channel_file = directory / f"{channel_slug}.md"
            if channel_file.exists():
                parts.append(_CHANNEL_HEADER + channel_file.read_text(encoding="utf-8"))
        return parts

    @staticmethod
    def _triggers(text: str) -> list[str] | None:
        first = text.lstrip().split("\n", 1)[0]
        if not first.startswith(WHEN_MARK):
            return None
        body = first[len(WHEN_MARK) :].split("-->", 1)[0]
        return [w.strip().lower() for w in body.split(",") if w.strip()]
