from pathlib import Path

from slack_cli_agent.prompt.knowledge import KnowledgeLoader


def _loader(tmp_path: Path, **kw) -> KnowledgeLoader:
    k = tmp_path / "knowledge"
    k.mkdir()
    (k / "_rules.md").write_text(
        "# 규칙\n- 늘 지킨다\n- 틀렸다 <!-- learn:2026-10-22 -->\n- 또 틀렸다 <!-- learn:2026-10-23 -->\n",
        encoding="utf-8",
    )
    (k / "ch.md").write_text("- 채널 규칙\n", encoding="utf-8")
    return KnowledgeLoader(tmp_path / "PERSONA.md", k, **kw)


def test_learn_lines_leave_the_prompt_with_a_pointer(tmp_path: Path) -> None:
    text = _loader(tmp_path).knowledge_text("ch")
    assert "늘 지킨다" in text and "채널 규칙" in text
    assert "learn:2026" not in text
    assert "학습 기록은 지침에서 뺐다" in text and "_rules.md" in text


def test_document_without_learn_lines_is_unchanged(tmp_path: Path) -> None:
    text = _loader(tmp_path).knowledge_text("ch")
    assert text.count("학습 기록") == 1


def test_flag_off_keeps_them(tmp_path: Path) -> None:
    text = _loader(tmp_path, defer_learn_lines=False).knowledge_text("ch")
    assert "learn:2026-10-22" in text
