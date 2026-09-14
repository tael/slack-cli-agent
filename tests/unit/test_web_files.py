"""웹 콘솔의 프롬프트·지식 파일 편집 계층.

FileEditor 는 <state>/prompts, <state>/persona/knowledge 같은 디렉터리를
브라우저에서 온 이름으로 읽고 쓴다. 이름은 신뢰할 수 없으므로 root 밖을
가리키는 요청을 거부하는 것이 핵심이다.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from slack_cli_agent.web.files import FileEditor, PathEscapeError


class Test이름_목록:
    def test_root이_없으면_빈_목록이다(self, tmp_path: Path) -> None:
        editor = FileEditor(tmp_path / "없음")
        assert editor.names() == []

    def test_확장자를_뗀_이름을_정렬해서_돌려준다(self, tmp_path: Path) -> None:
        (tmp_path / "b.md").write_text("나", encoding="utf-8")
        (tmp_path / "a.md").write_text("가", encoding="utf-8")
        (tmp_path / "c.txt").write_text("다른 확장자", encoding="utf-8")
        editor = FileEditor(tmp_path)
        assert editor.names() == ["a", "b"]


class Test읽기:
    def test_있는_파일을_읽는다(self, tmp_path: Path) -> None:
        (tmp_path / "greeting.md").write_text("안녕하세요", encoding="utf-8")
        editor = FileEditor(tmp_path)
        assert editor.read("greeting") == "안녕하세요"

    def test_없는_파일을_읽으면_예외다(self, tmp_path: Path) -> None:
        editor = FileEditor(tmp_path)
        with pytest.raises(PathEscapeError):
            editor.read("없는파일")


class Test경로_탈출_거부:
    def test_상위_디렉터리_참조를_거부한다(self, tmp_path: Path) -> None:
        root = tmp_path / "state" / "prompts"
        root.mkdir(parents=True)
        secret = tmp_path / "secret.md"
        secret.write_text("비밀", encoding="utf-8")
        editor = FileEditor(root)
        with pytest.raises(PathEscapeError):
            editor.read("../../secret")

    def test_절대_경로를_거부한다(self, tmp_path: Path) -> None:
        root = tmp_path / "state" / "prompts"
        root.mkdir(parents=True)
        outside = tmp_path / "outside.md"
        outside.write_text("바깥", encoding="utf-8")
        editor = FileEditor(root)
        with pytest.raises(PathEscapeError):
            editor.read(str(outside))

    def test_심볼릭_링크로_밖을_가리키면_거부한다(self, tmp_path: Path) -> None:
        root = tmp_path / "state" / "prompts"
        root.mkdir(parents=True)
        outside = tmp_path / "outside.md"
        outside.write_text("바깥", encoding="utf-8")
        link = root / "link.md"
        os.symlink(outside, link)
        editor = FileEditor(root)
        with pytest.raises(PathEscapeError):
            editor.read("link")

    def test_쓰기에도_같은_거부가_적용된다(self, tmp_path: Path) -> None:
        root = tmp_path / "state" / "prompts"
        root.mkdir(parents=True)
        editor = FileEditor(root)
        with pytest.raises(PathEscapeError):
            editor.write("../escape", "내용")


class Test쓰기:
    def test_임시_파일을_거쳐_교체한다(self, tmp_path: Path) -> None:
        editor = FileEditor(tmp_path)
        editor.write("guide", "새 내용")
        assert (tmp_path / "guide.md").read_text(encoding="utf-8") == "새 내용"
        assert list(tmp_path.glob("*.tmp")) == []

    def test_root이_없으면_만든다(self, tmp_path: Path) -> None:
        root = tmp_path / "state" / "prompts"
        editor = FileEditor(root)
        editor.write("guide", "내용")
        assert (root / "guide.md").read_text(encoding="utf-8") == "내용"

    def test_빈_본문_저장은_거부한다(self, tmp_path: Path) -> None:
        (tmp_path / "guide.md").write_text("기존 내용", encoding="utf-8")
        editor = FileEditor(tmp_path)
        with pytest.raises(ValueError):
            editor.write("guide", "")
        assert (tmp_path / "guide.md").read_text(encoding="utf-8") == "기존 내용"

    def test_공백만_있는_본문도_거부한다(self, tmp_path: Path) -> None:
        editor = FileEditor(tmp_path)
        with pytest.raises(ValueError):
            editor.write("guide", "   \n  ")
