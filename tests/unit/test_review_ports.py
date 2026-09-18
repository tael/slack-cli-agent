"""답변 사후 점검이 밖에 기대는 계약들.

Protocol 이 `runtime_checkable` 이어야 구현이 계약을 만족하는지 시험으로
확인할 수 있다. 그렇지 않으면 `isinstance` 가 TypeError 를 내서, 조립 계층이
계약을 어긴 객체를 넣어도 실행 시점까지 드러나지 않는다.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from slack_cli_agent.engine.base import EngineResponse
from slack_cli_agent.review.base import (
    AnswerRecordFinderPort,
    EngineCaller,
    MessageLookupPort,
    PermalinkPort,
    PublisherPort,
    ReactionPort,
    TranscriptPort,
)


class 엔진호출:
    model = "모델"
    effort = "medium"

    def run(self, prompt: str, session_id: str, resume: bool) -> EngineResponse:
        raise NotImplementedError


class 엔진호출아님:
    def call(self, prompt: str) -> None:
        return None


class 메시지조회:
    def find(self, channel: str, ts: str) -> Mapping[str, Any] | None:
        return None


class 표식:
    def mark_processing(self, channel: str, ts: str) -> None:
        return None

    def clear_processing(self, channel: str, ts: str) -> None:
        return None


class 표식절반:
    """`clear_processing` 이 없다. 처리 중 표식을 달기만 하고 못 지우는 구현이다."""

    def mark_processing(self, channel: str, ts: str) -> None:
        return None


class Test계약을_시험으로_확인할_수_있다:
    def test_엔진호출_계약을_만족한다(self) -> None:
        assert isinstance(엔진호출(), EngineCaller)

    def test_run_이_없으면_만족하지_않는다(self) -> None:
        assert not isinstance(엔진호출아님(), EngineCaller)

    def test_메시지조회_계약을_만족한다(self) -> None:
        assert isinstance(메시지조회(), MessageLookupPort)

    def test_표식_계약을_만족한다(self) -> None:
        assert isinstance(표식(), ReactionPort)

    def test_메서드가_하나_빠지면_만족하지_않는다(self) -> None:
        assert not isinstance(표식절반(), ReactionPort)

    def test_나머지_계약도_확인할_수_있다(self) -> None:
        """`isinstance` 가 TypeError 를 내지 않는 것 자체가 확인 대상이다."""
        for protocol in (TranscriptPort, AnswerRecordFinderPort, PermalinkPort, PublisherPort):
            assert not isinstance(object(), protocol)
