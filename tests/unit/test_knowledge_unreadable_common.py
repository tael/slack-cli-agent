"""읽히지 않는 공통 지식 파일은 요청을 멈추지 않는다.

`knowledge/_지식색인.md` 는 옵시디언 볼트(iCloud)를 가리키는 symlink 이고,
대상이 아직 내려오지 않은 상태에서 읽으면 `OSError: [Errno 11] Resource
deadlock avoided` 가 난다. 2026-11-24 11:15-11:17 에 이 예외가 시스템 프롬프트
조립 밖으로 나가 요청 3건이 모두 실패했다. 파일 하나를 못 읽는 것은 프롬프트의
빠진 자료이고, 답을 아예 못 내는 사유가 아니다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from slack_cli_agent.prompt.knowledge import KnowledgeLoader


def _loader(tmp_path: Path) -> KnowledgeLoader:
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "_common.md").write_text("공통 지식 원문", encoding="utf-8")
    (knowledge / "general.md").write_text("채널 지식 원문", encoding="utf-8")
    return KnowledgeLoader(tmp_path / "PERSONA.md", knowledge)


def _raise_oserror(_self: Path, **_kwargs: object) -> str:
    raise OSError(11, "Resource deadlock avoided")


def _unreadable_common(tmp_path: Path) -> Path:
    """읽기에서 `OSError` 를 내는 `_*.md`. 실제 사고는 iCloud 가 내려주지 않은
    symlink 대상이었고, 여기서는 같은 계열의 오류를 내는 디렉터리로 세운다."""
    broken = tmp_path / "knowledge" / "_색인.md"
    broken.mkdir()
    return broken


def test_공통_파일_읽기가_실패해도_나머지_지식이_실린다(tmp_path: Path) -> None:
    loader = _loader(tmp_path)
    _unreadable_common(tmp_path)

    text, _ = loader.knowledge_selection("general", "아무 요청")

    assert "공통 지식 원문" in text
    assert "채널 지식 원문" in text


def test_모든_공통_파일이_Errno11이어도_예외가_나가지_않는다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = _loader(tmp_path)
    monkeypatch.setattr(Path, "read_text", _raise_oserror)

    text, _ = loader.knowledge_selection("general", "아무 요청")

    assert isinstance(text, str)


def test_못_읽은_파일은_싣지_않은_목록에_경로로_남는다(tmp_path: Path) -> None:
    loader = _loader(tmp_path)
    broken = _unreadable_common(tmp_path)

    text, _ = loader.knowledge_selection("general", "아무 요청")

    assert str(broken) in text
