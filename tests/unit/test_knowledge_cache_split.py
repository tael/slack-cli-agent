"""지식 문서를 늘 실리는 것과 요청 낱말로 고른 것으로 나눈다 (S4 2회차).

요청 낱말로 고른 문서가 시스템 프롬프트 앞쪽에 있으면 같은 채널이라도
요청마다 앞부분이 달라져 프롬프트 캐시를 다시 쓴다.
"""

from __future__ import annotations

from pathlib import Path

from slack_cli_agent.prompt.knowledge import (
    KNOWLEDGE_FIXED,
    KNOWLEDGE_SELECTED,
    KnowledgeLoader,
)


def _loader(tmp_path: Path) -> KnowledgeLoader:
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "_common.md").write_text("공통 지식 원문", encoding="utf-8")
    (knowledge / "_airflow.md").write_text(
        "<!-- when: 에어플로우 -->\n에어플로우 지식 원문", encoding="utf-8",
    )
    (knowledge / "_metabase.md").write_text(
        "<!-- when: 메타베이스 -->\n메타베이스 지식 원문", encoding="utf-8",
    )
    (knowledge / "general.md").write_text("채널 지식 원문", encoding="utf-8")
    return KnowledgeLoader(tmp_path / "PERSONA.md", knowledge)


def test_늘_실리는_부분은_요청_낱말과_무관하게_같다(tmp_path: Path) -> None:
    loader = _loader(tmp_path)
    a, _ = loader.knowledge_selection("general", "에어플로우 봐줘", part=KNOWLEDGE_FIXED)
    b, _ = loader.knowledge_selection("general", "메타베이스 봐줘", part=KNOWLEDGE_FIXED)
    c, _ = loader.knowledge_selection("general", "", part=KNOWLEDGE_FIXED)
    assert a == b == c
    assert "공통 지식 원문" in a and "채널 지식 원문" in a
    assert "에어플로우 지식 원문" not in a and "메타베이스 지식 원문" not in a


def test_고른_부분에_고른_문서와_안_고른_목록이_간다(tmp_path: Path) -> None:
    loader = _loader(tmp_path)
    text, _ = loader.knowledge_selection("general", "에어플로우 봐줘", part=KNOWLEDGE_SELECTED)
    assert "에어플로우 지식 원문" in text
    assert "메타베이스 지식 원문" not in text
    assert str(tmp_path / "knowledge" / "_metabase.md") in text
    assert "공통 지식 원문" not in text


def test_두_부분을_합치면_예전_전체와_같은_문서가_실린다(tmp_path: Path) -> None:
    loader = _loader(tmp_path)
    whole, _ = loader.knowledge_selection("general", "에어플로우 봐줘")
    fixed, _ = loader.knowledge_selection("general", "에어플로우 봐줘", part=KNOWLEDGE_FIXED)
    picked, _ = loader.knowledge_selection("general", "에어플로우 봐줘", part=KNOWLEDGE_SELECTED)
    for chunk in whole.split("\n\n"):
        assert chunk in fixed or chunk in picked


def test_늘_싣는_스킬로_지정한_문서는_늘_실리는_부분이다(tmp_path: Path) -> None:
    loader = _loader(tmp_path)
    loader = KnowledgeLoader(
        tmp_path / "PERSONA.md", tmp_path / "knowledge", always_skills=("metabase",),
    )
    fixed, _ = loader.knowledge_selection("general", "", part=KNOWLEDGE_FIXED)
    assert "메타베이스 지식 원문" in fixed


def test_상한이_0이면_해당_부분_문서만_빠진_것으로_센다(tmp_path: Path) -> None:
    loader = _loader(tmp_path)
    text, omitted = loader.knowledge_selection(
        "general", "에어플로우", budget=0, part=KNOWLEDGE_SELECTED,
    )
    assert text == ""
    assert [d.path.name for d in omitted] == ["_airflow.md"]
