"""LearningService — 관리 명령이 호출할 상위 진입점.

원본 bot.py 의 show_proposal()/apply_learning()/revert_learning() 을 하나로
묶는다. 세 함수 모두 사람에게 보낼 메시지 문자열을 돌려주는 계약이었다 — 그
계약을 그대로 옮긴다. admin/ 은 이 서비스 하나만 호출하면 된다.
"""

from __future__ import annotations

from pathlib import Path

from slack_cli_agent.learning.apply import LearningApplier, LearningReverter
from slack_cli_agent.learning.proposal import LearningProposal, ProposalStore
from slack_cli_agent.learning.render import ProposalRenderer
from slack_cli_agent.learning.service import LearningService


def make_service(tmp_path: Path) -> LearningService:
    proposals = ProposalStore(tmp_path / "proposals")
    knowledge = tmp_path / "knowledge"
    return LearningService(
        store=proposals,
        applier=LearningApplier(knowledge, bot_name="봇"),
        reverter=LearningReverter(knowledge),
        renderer=ProposalRenderer(),
    )


def save_default_proposal(tmp_path: Path, day: str = "2026-09-14") -> None:
    ProposalStore(tmp_path / "proposals").save(
        LearningProposal(
            day=day,
            writing_style=("문장을 짧게 써라",),
            channel_knowledge={"공지": ("9월 회의는 매주 화요일이다",)},
            corrections=(),
        )
    )


class TestShowProposal:
    def test_제안이_없으면_안내한다(self, tmp_path: Path) -> None:
        service = make_service(tmp_path)
        assert service.show_proposal() == "아직 학습 제안이 없어요."

    def test_제안이_있으면_렌더링한다(self, tmp_path: Path) -> None:
        save_default_proposal(tmp_path)
        service = make_service(tmp_path)
        text = service.show_proposal()
        assert "9월 회의는 매주 화요일이다" in text
        assert "학습 반영" in text

    def test_깨진_제안_파일은_읽지_못했다고_안내한다(self, tmp_path: Path) -> None:
        proposals_dir = tmp_path / "proposals"
        proposals_dir.mkdir(parents=True)
        (proposals_dir / "2026-09-14.json").write_text("{깨짐", encoding="utf-8")
        service = make_service(tmp_path)
        text = service.show_proposal()
        assert "읽지 못했" in text


class TestApplyLatest:
    def test_제안이_없으면_안내한다(self, tmp_path: Path) -> None:
        service = make_service(tmp_path)
        assert service.apply_latest() == "반영할 학습 제안이 없어요."

    def test_반영하면_반영_결과와_되돌리기_안내를_담는다(self, tmp_path: Path) -> None:
        save_default_proposal(tmp_path)
        service = make_service(tmp_path)
        text = service.apply_latest()
        assert "2026-09-14 에 배운 걸 반영했어요." in text
        assert "공지 1건" in text
        assert "학습 되돌리기 2026-09-14" in text

    def test_두_번_반영하면_새로_반영할_것이_없다고_한다(self, tmp_path: Path) -> None:
        save_default_proposal(tmp_path)
        service = make_service(tmp_path)
        service.apply_latest()
        text = service.apply_latest()
        assert "새로 반영할 내용이 없어요" in text

    def test_반영하면_applied_기록을_남긴다(self, tmp_path: Path) -> None:
        save_default_proposal(tmp_path)
        service = make_service(tmp_path)
        service.apply_latest()
        assert (tmp_path / "proposals" / "2026-09-14.applied").exists()


class TestRevert:
    def test_날짜_형식이_아니면_안내한다(self, tmp_path: Path) -> None:
        service = make_service(tmp_path)
        assert "예 : 학습 되돌리기" in service.revert("아무거나")

    def test_반영된_게_없으면_안내한다(self, tmp_path: Path) -> None:
        service = make_service(tmp_path)
        assert service.revert("2026-09-14") == "2026-09-14 에 반영된 게 없어요."

    def test_반영한_뒤_되돌리면_지운_줄_수를_알려준다(self, tmp_path: Path) -> None:
        save_default_proposal(tmp_path)
        service = make_service(tmp_path)
        service.apply_latest()
        text = service.revert("2026-09-14")
        assert "2026-09-14 에 반영한" in text
        assert "줄을 지웠어요." in text
