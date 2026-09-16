"""학습 제안을 지식 파일에 반영/되돌리기.

원본 learn.py 의 apply_items()/apply_proposal()/revert() 를 대응한다.
각 줄 끝에 `<!-- learn:YYYY-MM-DD -->` 꼬리표를 달고, 되돌리기는 그 꼬리표가
붙은 줄만 지운다. 이미 파일에 있는 문장은 중복으로 넣지 않는다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from slack_cli_agent.learning.apply import LearningApplier, LearningReverter
from slack_cli_agent.learning.proposal import LearningProposal


def make_proposal(**overrides) -> LearningProposal:
    base: dict[str, Any] = {
        "day": "2026-09-14",
        "writing_style": ("문장을 짧게 써라",),
        "channel_knowledge": {"공지": ("9월 회의는 매주 화요일이다",)},
        "corrections": ("재고 조회는 /stock 이다",),
        "note": "",
    }
    base.update(overrides)
    return LearningProposal(**base)


class TestLearningApplier:
    def test_채널_지식이_채널_파일에_쌓인다(self, tmp_path: Path) -> None:
        applier = LearningApplier(tmp_path, bot_name="봇")
        done = applier.apply(make_proposal())
        text = (tmp_path / "공지.md").read_text(encoding="utf-8")
        assert "9월 회의는 매주 화요일이다" in text
        assert "<!-- learn:2026-09-14 -->" in text
        assert done["공지"] == 1

    def test_형식_교정은_밑줄_writing_style_파일에_쌓인다(self, tmp_path: Path) -> None:
        applier = LearningApplier(tmp_path, bot_name="봇")
        done = applier.apply(make_proposal())
        text = (tmp_path / "_writing-style.md").read_text(encoding="utf-8")
        assert "문장을 짧게 써라" in text
        assert done["_writing-style"] == 1

    def test_정정된_것은_밑줄_corrections_파일에_쌓인다(self, tmp_path: Path) -> None:
        applier = LearningApplier(tmp_path, bot_name="봇")
        done = applier.apply(make_proposal())
        text = (tmp_path / "_corrections.md").read_text(encoding="utf-8")
        assert "재고 조회는 /stock 이다" in text
        assert done["_corrections"] == 1

    def test_같은_문장은_두_번_들어가지_않는다(self, tmp_path: Path) -> None:
        applier = LearningApplier(tmp_path, bot_name="봇")
        applier.apply(make_proposal())
        done = applier.apply(make_proposal(day="2026-09-15"))
        assert done == {}
        text = (tmp_path / "공지.md").read_text(encoding="utf-8")
        assert text.count("9월 회의는 매주 화요일이다") == 1

    def test_빈_제안은_아무것도_쓰지_않는다(self, tmp_path: Path) -> None:
        applier = LearningApplier(tmp_path, bot_name="봇")
        empty = LearningProposal(day="2026-09-14")
        done = applier.apply(empty)
        assert done == {}
        assert not list(tmp_path.glob("*.md"))

    def test_쓰기는_임시_파일을_거쳐_교체한다(self, tmp_path: Path) -> None:
        applier = LearningApplier(tmp_path, bot_name="봇")
        applier.apply(make_proposal())
        assert not (tmp_path / "공지.md.tmp").exists()


class TestLearningReverter:
    def test_그날_꼬리표가_붙은_줄만_지운다(self, tmp_path: Path) -> None:
        applier = LearningApplier(tmp_path, bot_name="봇")
        applier.apply(make_proposal(day="2026-09-14"))
        applier.apply(make_proposal(
            day="2026-09-15",
            writing_style=(), corrections=(),
            channel_knowledge={"공지": ("10월엔 회의가 없다",)},
        ))
        reverter = LearningReverter(tmp_path)
        removed = reverter.revert("2026-09-14")
        assert removed > 0
        text = (tmp_path / "공지.md").read_text(encoding="utf-8")
        assert "9월 회의는 매주 화요일이다" not in text
        assert "10월엔 회의가 없다" in text

    def test_반영된_게_없으면_0을_돌려준다(self, tmp_path: Path) -> None:
        tmp_path.mkdir(exist_ok=True)
        reverter = LearningReverter(tmp_path)
        assert reverter.revert("2026-09-14") == 0

    def test_지식_디렉터리가_없어도_예외_없이_0이다(self, tmp_path: Path) -> None:
        reverter = LearningReverter(tmp_path / "없는곳")
        assert reverter.revert("2026-09-14") == 0
