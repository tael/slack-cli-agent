"""요청 처리의 공용 계약 — core/ports.py.

워커(큐를 소비하는 루프)와 파이프라인(요청 하나를 끝까지 처리)이 이 계약
하나로만 만난다. 워커가 파이프라인 구현을 직접 알면 큐 소비 로직을 시험할 때
엔진·슬랙까지 전부 대역으로 세워야 한다.
"""

from __future__ import annotations

import pytest

from slack_cli_agent.core.context import RequestContext
from slack_cli_agent.core.ports import HandleOutcome, RequestHandler


class 처리기:
    def handle(self, ctx: RequestContext) -> HandleOutcome:
        return HandleOutcome(ok=True)


class 처리기아님:
    def run(self, ctx: RequestContext) -> None:
        return None


class Test요청처리계약:
    def test_handle_을_가지면_계약을_만족한다(self) -> None:
        assert isinstance(처리기(), RequestHandler)

    def test_handle_이_없으면_계약을_만족하지_않는다(self) -> None:
        assert not isinstance(처리기아님(), RequestHandler)


class Test처리결과:
    def test_성공은_사유가_비어_있다(self) -> None:
        assert HandleOutcome(ok=True).failure == ""

    def test_실패는_사유를_담는다(self) -> None:
        outcome = HandleOutcome(ok=False, failure="엔진 시간 초과")
        assert not outcome.ok
        assert outcome.failure == "엔진 시간 초과"

    def test_올린_글의_시각을_담는다(self) -> None:
        """워커가 감시 작업 등록에 쓴다. 못 올렸으면 빈 문자열이다."""
        assert HandleOutcome(ok=True).posted_ts == ""
        assert HandleOutcome(ok=True, posted_ts="17.5").posted_ts == "17.5"

    def test_결과는_바뀌지_않는다(self) -> None:
        outcome = HandleOutcome(ok=True)
        with pytest.raises(Exception):
            outcome.ok = False  # type: ignore[misc]

    def test_침묵은_성공이되_올린_글이_없다(self) -> None:
        """답하지 않기로 한 것도 처리 실패가 아니다. 큐에서 완료로 빠진다."""
        outcome = HandleOutcome(ok=True, silent=True)
        assert outcome.ok and outcome.silent and outcome.posted_ts == ""
