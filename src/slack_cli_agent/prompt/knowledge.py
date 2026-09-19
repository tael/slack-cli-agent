# Files are reread on every request so edits take effect without a restart.

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
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
#: Left out for lack of room, not for lack of relevance. Kept apart from
#: _SKIPPED_HEADER because telling the model "this didn't match your words"
#: about a file that did match would be a lie (sca-ygd).
BUDGET_OMITTED_HEADER = (
    "# 자리가 없어 싣지 못한 지식\n\n"
    "아래 파일은 지침 상한에 걸려 이번에는 싣지 않았다.\n"
    "그 주제를 다루게 되면 파일을 직접 읽는다. 없는 것으로 답하지 않는다.\n\n"
)
_CHANNEL_HEADER = "# 이 채널의 지식\n\n앞에 적힌 것과 다르게 적힌 항목이 있으면 여기를 따른다.\n\n"
_LEARNED_HEADER = (
    "# 학습이 쌓은 지식\n\n"
    "대화에서 확인한 것을 날짜와 함께 쌓은 것이다. 사람이 쓴 지식과 어긋나면\n"
    "사람이 쓴 쪽을 따른다.\n\n"
)


@dataclass(frozen=True)
class KnowledgeDocument:
    """One knowledge file as it would be sent, with the path to name it by.

    The path is what a budget-omitted note points at, so the model can read
    the file itself instead of answering as if it did not exist.
    """

    path: Path
    text: str

    @property
    def size(self) -> int:
        return len(self.text.encode("utf-8"))


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
        return self.knowledge_within(channel_slug, prompt, budget=None)

    def knowledge_within(
        self, channel_slug: str, prompt: str = "", budget: int | None = None,
    ) -> str:
        """The same text, dropped to whole documents that fit in `budget` bytes.

        Whole documents rather than a character cut: a knowledge file is one
        unit of meaning and cutting it mid-sentence detaches the exception from
        the rule it belongs to. None means no cap, which is the shape every
        caller had before the budget existed (sca-ygd).
        """
        documents, skipped = self.documents(channel_slug, prompt)
        kept, omitted = _within_budget(documents, budget)
        parts = [doc.text for doc in kept]
        if skipped:
            parts.append(
                _SKIPPED_HEADER + "\n".join(f"- {p.stem.lstrip('_')} : {p}" for p in skipped)
            )
        if omitted:
            parts.append(
                BUDGET_OMITTED_HEADER
                + "\n".join(f"- {doc.path.stem.lstrip('_')} : {doc.path}" for doc in omitted)
            )
        return "\n\n".join(parts).strip()

    def documents(
        self, channel_slug: str, prompt: str = "",
    ) -> tuple[tuple[KnowledgeDocument, ...], tuple[Path, ...]]:
        """Every document this request would carry, in send order.

        Split out so the caller can decide what fits; the loader stays the one
        place that knows where documents come from and how they are labelled.
        """
        skipped: list[Path] = []
        documents = list(self._from_dir(self._knowledge_dir, channel_slug, prompt, skipped))
        learned = (
            self._from_dir(self._learned_dir, channel_slug, prompt, skipped)
            if self._learned_dir is not None
            else []
        )
        if learned:
            merged = _LEARNED_HEADER + "\n\n".join(doc.text for doc in learned)
            documents.append(KnowledgeDocument(path=learned[0].path, text=merged))
        return tuple(documents), tuple(skipped)

    def _from_dir(
        self, directory: Path, channel_slug: str, prompt: str, skipped: list[Path],
    ) -> list[KnowledgeDocument]:
        parts: list[KnowledgeDocument] = []
        low = (prompt or "").lower()
        if directory.is_dir():
            for common in sorted(directory.glob("_*.md")):
                text = common.read_text(encoding="utf-8")
                triggers = self._triggers(text)
                if triggers is not None and not any(t in low for t in triggers):
                    skipped.append(common)
                    continue
                parts.append(KnowledgeDocument(path=common, text=text))
        if channel_slug:
            channel_file = directory / f"{channel_slug}.md"
            if channel_file.exists():
                parts.append(KnowledgeDocument(
                    path=channel_file,
                    text=_CHANNEL_HEADER + channel_file.read_text(encoding="utf-8"),
                ))
        return parts

    @staticmethod
    def _triggers(text: str) -> list[str] | None:
        first = text.lstrip().split("\n", 1)[0]
        if not first.startswith(WHEN_MARK):
            return None
        body = first[len(WHEN_MARK) :].split("-->", 1)[0]
        return [w.strip().lower() for w in body.split(",") if w.strip()]


def _within_budget(
    documents: Sequence[KnowledgeDocument], budget: int | None,
) -> tuple[list[KnowledgeDocument], list[KnowledgeDocument]]:
    """Keeps documents in send order until the next one would not fit.

    A later small document is still taken after a large one was dropped: the
    order is a priority, and skipping one oversized file should not cost the
    ones behind it.
    """
    if budget is None:
        return list(documents), []
    kept: list[KnowledgeDocument] = []
    omitted: list[KnowledgeDocument] = []
    used = 0
    for doc in documents:
        if used + doc.size <= budget:
            kept.append(doc)
            used += doc.size
        else:
            omitted.append(doc)
    return kept, omitted
