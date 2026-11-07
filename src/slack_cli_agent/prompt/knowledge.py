# Files are reread on every request so edits take effect without a restart.

from __future__ import annotations

import dataclasses
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path

# A knowledge file whose first line starts with this is loaded only if the
# request text contains one of the trigger words listed after it; files
# without this marker are always loaded.
WHEN_MARK = "<!-- when:"
#: A line the nightly learner appended ("- ... <!-- learn:2026-10-22 -->").
#: These are dated incident notes, not standing rules, and they were 41% of
#: an owner turn's always-loaded knowledge (135KB of 330KB, 2026-11-07 실측).
#: Every API call in a turn re-reads the whole system prompt, and a 159k-token
#: cached prefix measured 6.4-8.5s per call against 1.1-2.2s for a 2.8k one,
#: so they leave the prompt and stay one Grep away in the file they live in.
LEARN_MARK = "<!-- learn:"

#: The wiki index the knowledge mirror writes into `knowledge/` for a loader
#: that could not read `wiki/index.md` itself. Once the loader reads the index,
#: this copy would put the same text in the prompt twice, so it is skipped
#: whenever the index itself is there (H5).
WIKI_INDEX_MIRROR = "_위키목차.md"

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
#: A per-bot channel rule that the knowledge mirror must not overwrite. The
#: mirror copies a sibling bot's `knowledge/` over this bot's every night, so a
#: rule meant for one bot only lives in `local/` instead and loads last, over
#: everything the mirror could have replaced (마메치 load_persona 대응).
_LOCAL_HEADER = (
    "# 이 봇만의 채널 규정\n\n앞에 적힌 것과 다르게 적힌 항목이 있으면 여기를 따른다.\n\n"
)
_LEARNED_HEADER = (
    "# 학습이 쌓은 지식\n\n"
    "대화에서 확인한 것을 날짜와 함께 쌓은 것이다. 사람이 쓴 지식과 어긋나면\n"
    "사람이 쓴 쪽을 따른다.\n\n"
)


@dataclass(frozen=True)
class KnowledgeDocument:
    """One knowledge file as it would be sent, with the path to name it by.

    The path is what a budget-omitted note points at, so the model can read
    the file itself instead of answering as if it did not exist. Documents
    stay separate all the way to rendering: merging several into one would
    drop them together and name only the first in that note.
    """

    path: Path
    text: str
    #: Learned knowledge gets one shared header, printed once above the first
    #: of them that survives the budget.
    learned: bool = False

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
        local_dir: Path | None = None,
        always_skills: Collection[str] = (),
        wiki_dir: Path | None = None,
        defer_learn_lines: bool = True,
    ) -> None:
        # True keeps learner-appended lines out of the prompt and leaves a
        # pointer to grep them (LEARN_MARK). False is the pre-2026-11-07 shape.
        self._defer_learn_lines = defer_learn_lines
        self._persona_file = persona_file
        self._knowledge_dir = knowledge_dir
        self._domain_file = domain_file
        # None means this bot has no separate learned directory, which is the
        # shape every caller had before the two sources were split.
        self._learned_dir = learned_dir
        # None means this bot keeps no mirror-exempt overlay. When set, a
        # `<channel>.md` here loads last, after knowledge and learned, so it wins
        # over anything the nightly mirror could have replaced. Channel-scoped
        # only: common `_*.md` belongs in the mirrored `knowledge/` (sca).
        self._local_dir = local_dir
        # Common knowledge files named after one of these (stem with its
        # leading "_" stripped, e.g. "_metabase.md" -> "metabase") load even
        # when their own `when:` marker doesn't match this request's words.
        # A trigger match is one way in; standing clearance named here is
        # another, and the two don't cancel each other out.
        self._always_skills = frozenset(always_skills)
        # None means this bot keeps no wiki. When set, `index.md` here loads on
        # every turn regardless of `when:` words, after common knowledge and
        # before the channel file; the pages it lists are opened by the model.
        self._wiki_dir = wiki_dir

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
        return self.knowledge_selection(channel_slug, prompt, budget)[0]

    def knowledge_selection(
        self, channel_slug: str, prompt: str = "", budget: int | None = None,
    ) -> tuple[str, tuple[KnowledgeDocument, ...]]:
        """The text plus what the budget left out, for the caller that has to
        report it. A budget of 0 or less renders nothing at all -- printing the
        omission list there would add bytes to a prompt that already has none
        to spare (리뷰 2026-09-19)."""
        if budget is not None and budget <= 0:
            documents, _ = self.documents(channel_slug, prompt)
            return "", documents
        documents, skipped = self.documents(channel_slug, prompt)
        kept, omitted = _within_budget(documents, budget, skipped)
        return _render(kept, omitted, skipped), tuple(omitted)

    def documents(
        self, channel_slug: str, prompt: str = "",
    ) -> tuple[tuple[KnowledgeDocument, ...], tuple[Path, ...]]:
        """Every document this request would carry, in send order.

        Split out so the caller can decide what fits; the loader stays the one
        place that knows where documents come from and how they are labelled.
        """
        skipped: list[Path] = []
        wiki = self._wiki_index()
        documents = list(self._from_dir(
            self._knowledge_dir, channel_slug, prompt, skipped,
            after_common=wiki, skip_names={WIKI_INDEX_MIRROR} if wiki else frozenset(),
        ))
        learned = (
            self._from_dir(self._learned_dir, channel_slug, prompt, skipped)
            if self._learned_dir is not None
            else []
        )
        documents += [dataclasses.replace(doc, learned=True) for doc in learned]
        documents += self._local_overlay(channel_slug)
        if self._defer_learn_lines:
            documents = [_defer_learn_lines(doc) for doc in documents]
        return tuple(documents), tuple(skipped)

    def _local_overlay(self, channel_slug: str) -> list[KnowledgeDocument]:
        """The per-bot channel rule, headed and placed last. Empty when there is
        no local directory, no channel, or no file: a missing overlay is the
        normal case, not an error."""
        if self._local_dir is None or not channel_slug:
            return []
        local_file = self._local_dir / f"{channel_slug}.md"
        text = _read_or_none(local_file)
        if text is None:
            return []
        return [KnowledgeDocument(path=local_file, text=_LOCAL_HEADER + text)]

    def _wiki_index(self) -> list[KnowledgeDocument]:
        """The wiki index with `~/` paths spelled out. Read takes absolute paths
        only, so a `~/.<bot>/persona/wiki/<page>.md` left as written is a page
        the model cannot open (마메치 load_persona 의 replace 대응)."""
        if self._wiki_dir is None:
            return []
        index = self._wiki_dir / "index.md"
        text = _read_or_none(index)
        if text is None:
            return []
        home = Path.home()
        try:
            tilde = "~/" + str(self._wiki_dir.relative_to(home))
        except ValueError:
            tilde = None
        if tilde is not None:
            text = text.replace(tilde, str(self._wiki_dir))
        return [KnowledgeDocument(path=index, text=text)]

    def _from_dir(
        self, directory: Path, channel_slug: str, prompt: str, skipped: list[Path],
        after_common: Sequence[KnowledgeDocument] = (),
        skip_names: Collection[str] = frozenset(),
    ) -> list[KnowledgeDocument]:
        parts: list[KnowledgeDocument] = []
        low = (prompt or "").lower()
        if directory.is_dir():
            for common in sorted(directory.glob("_*.md")):
                if common.name in skip_names:
                    continue
                text = common.read_text(encoding="utf-8")
                triggers = self._triggers(text)
                always = common.stem.lstrip("_") in self._always_skills
                if triggers is not None and not always and not any(t in low for t in triggers):
                    skipped.append(common)
                    continue
                parts.append(KnowledgeDocument(path=common, text=text))
        parts.extend(after_common)
        if channel_slug:
            channel_file = directory / f"{channel_slug}.md"
            channel_text = _read_or_none(channel_file)
            if channel_text is not None:
                parts.append(KnowledgeDocument(
                    path=channel_file,
                    text=_CHANNEL_HEADER + channel_text,
                ))
        return parts

    @staticmethod
    def _triggers(text: str) -> list[str] | None:
        first = text.lstrip().split("\n", 1)[0]
        if not first.startswith(WHEN_MARK):
            return None
        body = first[len(WHEN_MARK) :].split("-->", 1)[0]
        return [w.strip().lower() for w in body.split(",") if w.strip()]


def _defer_learn_lines(doc: KnowledgeDocument) -> KnowledgeDocument:
    """The document without its learner lines, plus one line saying where
    they went. Unchanged when it has none."""
    lines = doc.text.split("\n")
    kept = [line for line in lines if LEARN_MARK not in line]
    moved = len(lines) - len(kept)
    if moved == 0:
        return doc
    note = (
        f"\n(학습 기록 {moved}줄은 지침에서 뺐다. 이 주제의 과거 정정이 필요하면 "
        f"Grep 으로 {doc.path} 에서 키워드와 'learn:' 을 찾는다.)\n"
    )
    return dataclasses.replace(doc, text="\n".join(kept).rstrip() + "\n" + note)


def _read_or_none(path: Path) -> str | None:
    """None when the file is not there to read. A slug migration renames this
    file while a request is being served, so exists() then read_text() can hit
    the gap between the two (sca-8aow), and a missing knowledge file must not
    stop the reply."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _render(
    kept: Sequence[KnowledgeDocument],
    omitted: Sequence[KnowledgeDocument],
    skipped: Sequence[Path],
) -> str:
    parts: list[str] = []
    for i, doc in enumerate(kept):
        first_learned = doc.learned and not any(d.learned for d in kept[:i])
        parts.append(_LEARNED_HEADER + doc.text if first_learned else doc.text)
    if skipped:
        parts.append(_SKIPPED_HEADER + _name_lines(skipped))
    if omitted:
        parts.append(BUDGET_OMITTED_HEADER + _name_lines([doc.path for doc in omitted]))
    return "\n\n".join(parts).strip()


def _name_lines(paths: Sequence[Path]) -> str:
    return "\n".join(f"- {path.stem.lstrip('_')} : {path}" for path in paths)


def _within_budget(
    documents: Sequence[KnowledgeDocument],
    budget: int | None,
    skipped: Sequence[Path] = (),
) -> tuple[list[KnowledgeDocument], list[KnowledgeDocument]]:
    """Picks the documents whose rendered text fits in `budget` bytes.

    Not a prefix cut: a document that doesn't fit is skipped and the ones
    behind it are still considered. File order is alphabetical, not a
    priority, so one oversized file must not cost everything after it.

    The size measured is the rendered text, headers and blank lines included.
    Counting document bodies alone let the omission note push the result past
    the budget it was supposed to keep (리뷰 2026-09-19).
    """
    if budget is None:
        return list(documents), []
    kept: list[KnowledgeDocument] = []
    omitted: list[KnowledgeDocument] = []
    for doc in documents:
        candidate = [*kept, doc]
        rest = [d for d in documents if d not in candidate]
        if _size(_render(candidate, [*omitted, *rest], skipped)) <= budget:
            kept.append(doc)
        else:
            omitted.append(doc)
    return kept, omitted


def _size(text: str) -> int:
    return len(text.encode("utf-8"))
