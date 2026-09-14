"""학습 제안 값 객체와 저장소.

원본 bot.py 의 latest_proposal() 을 대응한다. 파일 유무(부재)와 JSON 파싱
실패(판정 불가)를 Outcome 으로 구분한다 — 원본은 둘 다 None 으로 뭉뚱그려
"학습 제안이 없다"로 보였다.
"""

from __future__ import annotations

import json
from pathlib import Path

from slack_cli_agent.core.result import OutcomeKind
from slack_cli_agent.learning.proposal import LearningProposal, ProposalStore


def make_data(**overrides):
    data = {
        "writing_style": ["문장을 짧게 써라"],
        "channel_knowledge": {"공지": ["9월 회의는 매주 화요일이다"]},
        "corrections": ["재고 조회는 /stock 이다"],
        "note": "",
    }
    data.update(overrides)
    return data


class TestLearningProposal:
    def test_from_dict가_필드를_그대로_옮긴다(self) -> None:
        proposal = LearningProposal.from_dict("2026-09-14", make_data())
        assert proposal.day == "2026-09-14"
        assert proposal.writing_style == ("문장을 짧게 써라",)
        assert proposal.channel_knowledge == {"공지": ("9월 회의는 매주 화요일이다",)}
        assert proposal.corrections == ("재고 조회는 /stock 이다",)

    def test_없는_키는_빈_값으로_채운다(self) -> None:
        proposal = LearningProposal.from_dict("2026-09-14", {})
        assert proposal.writing_style == ()
        assert proposal.channel_knowledge == {}
        assert proposal.corrections == ()
        assert proposal.note == ""

    def test_has_content는_아무것도_없으면_거짓이다(self) -> None:
        empty = LearningProposal.from_dict("2026-09-14", {})
        assert empty.has_content is False

    def test_has_content는_채널_지식만_있어도_참이다(self) -> None:
        proposal = LearningProposal.from_dict(
            "2026-09-14", {"channel_knowledge": {"공지": ["사실 하나"]}}
        )
        assert proposal.has_content is True

    def test_to_dict는_from_dict의_역이다(self) -> None:
        data = make_data()
        proposal = LearningProposal.from_dict("2026-09-14", data)
        assert proposal.to_dict() == data


class TestProposalStore:
    def test_디렉터리가_없으면_부재다(self, tmp_path: Path) -> None:
        store = ProposalStore(tmp_path / "proposals")
        outcome = store.latest()
        assert outcome.kind is OutcomeKind.ABSENT

    def test_파일이_없으면_부재다(self, tmp_path: Path) -> None:
        tmp_path.mkdir(exist_ok=True)
        store = ProposalStore(tmp_path)
        outcome = store.latest()
        assert outcome.kind is OutcomeKind.ABSENT

    def test_가장_최근_날짜_파일을_읽는다(self, tmp_path: Path) -> None:
        (tmp_path / "2026-09-10.json").write_text(
            json.dumps(make_data(note="옛날 것")), encoding="utf-8"
        )
        (tmp_path / "2026-09-14.json").write_text(
            json.dumps(make_data(note="최근 것")), encoding="utf-8"
        )
        store = ProposalStore(tmp_path)
        outcome = store.latest()
        assert outcome.is_found
        proposal = outcome.value()
        assert proposal.day == "2026-09-14"
        assert proposal.note == "최근 것"

    def test_깨진_json은_판정_불가다(self, tmp_path: Path) -> None:
        (tmp_path / "2026-09-14.json").write_text("{이건 json이 아니다", encoding="utf-8")
        store = ProposalStore(tmp_path)
        outcome = store.latest()
        assert outcome.kind is OutcomeKind.UNKNOWN

    def test_save는_day_json_파일로_원자적으로_쓴다(self, tmp_path: Path) -> None:
        store = ProposalStore(tmp_path)
        proposal = LearningProposal.from_dict("2026-09-14", make_data())
        path = store.save(proposal)
        assert path == tmp_path / "2026-09-14.json"
        assert json.loads(path.read_text(encoding="utf-8")) == make_data()
        # 임시 파일이 남지 않는다
        assert not (tmp_path / "2026-09-14.json.tmp").exists()

    def test_save한_뒤_latest로_그대로_읽힌다(self, tmp_path: Path) -> None:
        store = ProposalStore(tmp_path)
        store.save(LearningProposal.from_dict("2026-09-14", make_data()))
        outcome = store.latest()
        assert outcome.is_found
        assert outcome.value().day == "2026-09-14"

    def test_mark_applied는_applied_파일을_남긴다(self, tmp_path: Path) -> None:
        store = ProposalStore(tmp_path)
        store.mark_applied("2026-09-14", {"공지": 2})
        applied = json.loads((tmp_path / "2026-09-14.applied").read_text(encoding="utf-8"))
        assert applied["done"] == {"공지": 2}
        assert "applied_at" in applied
