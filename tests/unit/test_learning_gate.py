"""학습 분석 게이트 판정 시험.

기록량이 적은 채널-일은 LLM 분석을 건너뛰고 이월한다. 이 파일은 순수 판정과
이월 목록 저장을 다룬다. 배치와 엮인 흐름은 test_learning_batch.py 에 있다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from slack_cli_agent.learning.gate import (
    CarryStore,
    GatePolicy,
    LearningGate,
    count_entries,
    has_correction_signal,
)


def entry(question: str, answer: str = "알겠습니다", stamp: str = "10:00") -> str:
    return (
        f"\n## {stamp} KST · 성공\n\n- 질문자 : U1\n- 스레드 : 1.1\n- 소요 : 1.0초, 1턴\n\n"
        f"### 질문\n\n{question}\n\n### 응답\n\n{answer}\n"
    )


def make_gate(min_entries: int = 3, max_days: int = 7) -> LearningGate:
    return LearningGate(GatePolicy(min_entries=min_entries, max_defer_days=max_days))


class TestEntryAndSignal:
    def test_기록_건수는_헤더_수다(self) -> None:
        text = entry("가") + entry("나") + entry("다")
        assert count_entries(text) == 3
        assert count_entries("") == 0

    def test_질문의_정정_표현을_신호로_본다(self) -> None:
        assert has_correction_signal(entry("아니다. 그건 잘못 알고 있다"))
        assert has_correction_signal(entry("이 말은 하지마"))

    def test_응답_안의_같은_표현은_신호가_아니다(self) -> None:
        assert not has_correction_signal(entry("시각 알려줘", answer="아니요, 잘못된 값입니다"))

    def test_흔한_요청어는_신호가_아니다(self) -> None:
        assert not has_correction_signal(entry("다시 한 번 왜 그런지 알려줘"))


class TestDecide:
    def test_기록이_적고_신호가_없으면_이월한다(self) -> None:
        decision = make_gate().decide(day="2026-11-15", carried_days=(), entries=2, signal=False)
        assert decision.analyze is False
        assert "2건" in decision.reason

    def test_정정_신호가_있으면_양과_무관하게_분석한다(self) -> None:
        decision = make_gate().decide(day="2026-11-15", carried_days=(), entries=1, signal=True)
        assert decision.analyze is True
        assert "정정 신호" in decision.reason

    def test_누적량이_기준에_닿으면_분석한다(self) -> None:
        decision = make_gate().decide(
            day="2026-11-15", carried_days=("2026-11-14",), entries=3, signal=False,
        )
        assert decision.analyze is True

    def test_최대_간격이_지나면_양과_무관하게_분석한다(self) -> None:
        gate = make_gate(max_days=7)
        assert gate.decide(
            day="2026-11-15", carried_days=("2026-11-09",), entries=1, signal=False,
        ).analyze is False
        late = gate.decide(day="2026-11-15", carried_days=("2026-11-08",), entries=1, signal=False)
        assert late.analyze is True
        assert "7일" in late.reason

    def test_기준을_1로_두면_게이트가_꺼진_것과_같다(self) -> None:
        assert make_gate(min_entries=1).decide(
            day="2026-11-15", carried_days=(), entries=1, signal=False,
        ).analyze is True

    def test_최대_이월_일수가_1_미만이면_거부한다(self) -> None:
        with pytest.raises(ValueError):
            GatePolicy(min_entries=3, max_defer_days=0)

    def test_기한이_찬_이월_채널을_찾는다(self) -> None:
        gate = make_gate(max_days=7)
        carry = {"a": ("2026-11-08",), "b": ("2026-11-12",), "c": ("2026-11-15",)}
        assert gate.due_channels("2026-11-15", carry) == {"a"}


class TestCarryStore:
    def test_추가하고_지운다(self, tmp_path: Path) -> None:
        store = CarryStore(tmp_path / "carry.json")
        store.add("a", "2026-11-13")
        store.add("a", "2026-11-14")
        store.add("b", "2026-11-14")
        assert store.load() == {"a": ("2026-11-13", "2026-11-14"), "b": ("2026-11-14",)}
        store.clear("a", ("2026-11-13", "2026-11-14"))
        assert store.load() == {"b": ("2026-11-14",)}

    def test_파일이_없으면_비어_있다(self, tmp_path: Path) -> None:
        assert CarryStore(tmp_path / "none.json").load() == {}

    def test_깨진_파일은_따로_보관하고_비워서_시작한다(self, tmp_path: Path) -> None:
        path = tmp_path / "carry.json"
        path.write_text("{깨짐", encoding="utf-8")
        assert CarryStore(path).load() == {}
        assert (tmp_path / "carry.json.corrupt").read_text(encoding="utf-8") == "{깨짐"
        assert not path.exists()

    def test_저장은_json_사전이다(self, tmp_path: Path) -> None:
        path = tmp_path / "carry.json"
        CarryStore(path).add("a", "2026-11-14")
        assert json.loads(path.read_text(encoding="utf-8")) == {"a": ["2026-11-14"]}
