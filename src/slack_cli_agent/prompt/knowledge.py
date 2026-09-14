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


class KnowledgeLoader:
    def __init__(
        self,
        persona_file: Path,
        knowledge_dir: Path,
        domain_file: Path | None = None,
    ) -> None:
        self._persona_file = persona_file
        self._knowledge_dir = knowledge_dir
        self._domain_file = domain_file

    def persona_text(self, include_domain: bool = False) -> str:
        parts: list[str] = []
        if self._persona_file.exists():
            parts.append(self._persona_file.read_text(encoding="utf-8"))
        if include_domain and self._domain_file and self._domain_file.exists():
            parts.append(self._domain_file.read_text(encoding="utf-8"))
        return "\n\n".join(parts).strip()

    def knowledge_text(self, channel_slug: str, prompt: str = "") -> str:
        parts: list[str] = []
        low = (prompt or "").lower()
        skipped: list[Path] = []
        if self._knowledge_dir.is_dir():
            for common in sorted(self._knowledge_dir.glob("_*.md")):
                text = common.read_text(encoding="utf-8")
                triggers = self._triggers(text)
                if triggers is not None and not any(t in low for t in triggers):
                    skipped.append(common)
                    continue
                parts.append(text)
        if skipped:
            parts.append(
                _SKIPPED_HEADER + "\n".join(f"- {p.stem.lstrip('_')} : {p}" for p in skipped)
            )
        if channel_slug:
            channel_file = self._knowledge_dir / f"{channel_slug}.md"
            if channel_file.exists():
                parts.append(_CHANNEL_HEADER + channel_file.read_text(encoding="utf-8"))
        return "\n\n".join(parts).strip()

    @staticmethod
    def _triggers(text: str) -> list[str] | None:
        first = text.lstrip().split("\n", 1)[0]
        if not first.startswith(WHEN_MARK):
            return None
        body = first[len(WHEN_MARK) :].split("-->", 1)[0]
        return [w.strip().lower() for w in body.split(",") if w.strip()]
