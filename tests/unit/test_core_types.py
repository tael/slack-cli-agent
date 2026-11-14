"""요청 맥락과 조회 결과 타입."""

from __future__ import annotations

import pytest

from slack_cli_agent.core.context import RequestContext
from slack_cli_agent.core.result import Outcome


def ctx(**over) -> RequestContext:
    base = {"channel": "C1", "user": "U1", "ts": "1.000001",
            "thread_ts": "1.000001", "text": "본문"}
    return RequestContext(**{**base, **over})


class TestOutcome:
    def test_부재는_기본값으로_대체된다(self) -> None:
        assert Outcome[list[str]].absent().value_or([]) == []

    def test_판정_불가는_기본값으로_대체되지_않는다(self) -> None:
        with pytest.raises(ValueError, match="판정 불가"):
            Outcome[list[str]].unknown("조회 3회 실패").value_or([])

    def test_판정_불가는_사유를_보존한다(self) -> None:
        outcome: Outcome[list[str]] = Outcome.unknown("조회 3회 실패")
        assert outcome.is_unknown
        assert outcome.reason == "조회 3회 실패"

    def test_값을_찾으면_그대로_돌려준다(self) -> None:
        assert Outcome.found(["a"]).value() == ["a"]

    def test_부재에서_값을_꺼내면_예외다(self) -> None:
        with pytest.raises(ValueError):
            Outcome.absent().value()


class TestRequestContext:
    def test_JSON_왕복이_손실이_없다(self) -> None:
        original = ctx(files=({"id": "F1"},), unaddressed=True, queued_at=12.5)
        assert RequestContext.from_json(original.to_json()) == original

    def test_언퍼얼_첨부도_JSON_왕복에서_튜플로_살아남는다(self) -> None:
        """링크 미리보기 첨부(C39 폴백용)는 ingress 가 큐에 적재할 때 함께 직렬화돼
        worker 의 파이프라인까지 가야 한다. files 와 같은 방식으로 튜플 복원한다."""
        original = ctx(unfurl_attachments=({"from_url": "https://x", "text": "본문"},))
        restored = RequestContext.from_json(original.to_json())
        assert restored == original
        assert isinstance(restored.unfurl_attachments, tuple)

    def test_중복_판정_키는_채널과_메시지_ts_다(self) -> None:
        assert ctx(channel="C1", ts="1.1").key == ("C1", "1.1")

    def test_캐치업_표시는_원본을_바꾸지_않는다(self) -> None:
        original = ctx()
        marked = original.marked_late()
        assert marked.late is True
        assert original.late is False

    def test_대기줄_재진입은_등록_시각을_남긴다(self) -> None:
        marked = ctx().marked_requeued(queued_at=100.0)
        assert marked.requeued is True
        assert marked.queued_at == 100.0
