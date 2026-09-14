"""조건부 지식 적재.

원본 load_persona 를 둘로 나눈다. 페르소나·도메인 사실은 persona_text 가,
공통 지식과 채널별 지식은 knowledge_text 가 맡는다. 두 메서드로 나누는 것은
PromptSection 을 PersonaSection·KnowledgeSection 둘로 쪼개는 것과 같은 이유다.

파일은 요청마다 다시 읽는다. 재기동 없이 고친 내용이 반영되어야 한다.
"""

from __future__ import annotations

from pathlib import Path

# 공통 지식 파일 첫 줄에 이 표식이 있으면 조건 적재로 다룬다.
# 표식 뒤 낱말이 요청 본문에 하나라도 있을 때만 싣는다.
# 표식이 없는 파일은 주제와 무관하게 늘 실린다.
WHEN_MARK = "<!-- when:"

_SKIPPED_HEADER = (
    "# 지금 싣지 않은 주제별 지식\n\n"
    "아래 파일은 이번 요청 낱말과 맞지 않아 싣지 않았다.\n"
    "그 주제를 다루게 되면 파일을 직접 읽는다. 없는 것으로 답하지 않는다.\n\n"
)
_CHANNEL_HEADER = "# 이 채널의 지식\n\n앞에 적힌 것과 다르게 적힌 항목이 있으면 여기를 따른다.\n\n"


class KnowledgeLoader:
    """페르소나와 지식 파일을 읽는다."""

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
        """페르소나 본문과, include_domain 이 참이면 사내 도메인 사실을 더한다."""
        parts: list[str] = []
        if self._persona_file.exists():
            parts.append(self._persona_file.read_text(encoding="utf-8"))
        if include_domain and self._domain_file and self._domain_file.exists():
            parts.append(self._domain_file.read_text(encoding="utf-8"))
        return "\n\n".join(parts).strip()

    def knowledge_text(self, channel_slug: str, prompt: str = "") -> str:
        """공통 지식(조건 적재)과 채널별 지식을 합친다.

        조건이 맞지 않아 싣지 않은 파일은 이름과 경로만 남겨 모델이 필요하면
        직접 열게 한다.
        """
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
