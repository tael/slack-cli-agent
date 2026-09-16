"""중단된 점검을 보고 채널에 알린다(sca-9bq).

재기동으로 점검이 죽으면 원장에 진행 행만 남고 아무 데도 흔적이 없다.
훑어서 알리고, 알린 행은 지워 다음 순회에 다시 안 나오게 한다.
"""

from __future__ import annotations

import pytest

from slack_cli_agent.review.ledger import ReviewLedger
from slack_cli_agent.review.stale_reporter import StaleReviewReporter


class _게시:
    def __init__(self, 실패: bool = False) -> None:
        self.보낸것: list[tuple[str, str | None, str, bool]] = []
        self._실패 = 실패

    def post(self, channel: str, thread_ts: str | None, text: str, *, rich: bool) -> str | None:
        self.보낸것.append((channel, thread_ts, text, rich))
        return None if self._실패 else "1.0"


@pytest.fixture
def 시각() -> dict[str, float]:
    return {"값": 1000.0}


@pytest.fixture
def 원장(database, 시각) -> ReviewLedger:
    return ReviewLedger(database, now=lambda: 시각["값"], stale_after_sec=100.0)


def _보고기(원장, 게시, 채널: str = "CTROUBLE") -> StaleReviewReporter:
    return StaleReviewReporter(원장, 게시, 채널)


def test_중단된_점검을_보고_채널에_알린다(원장, 시각) -> None:
    원장.begin("postmortem", "C1", "111.1", by="U1")
    시각["값"] = 1200.0
    게시 = _게시()
    _보고기(원장, 게시).sweep()
    assert len(게시.보낸것) == 1
    채널, thread_ts, 본문, _ = 게시.보낸것[0]
    assert (채널, thread_ts) == ("CTROUBLE", None)
    assert "C1" in 본문 and "111.1" in 본문


def test_알린_행은_지워_다음_순회에_안_나온다(원장, 시각) -> None:
    원장.begin("postmortem", "C1", "111.1", by="U1")
    시각["값"] = 1200.0
    게시 = _게시()
    보고기 = _보고기(원장, 게시)
    보고기.sweep()
    보고기.sweep()
    assert len(게시.보낸것) == 1
    assert 원장.find("postmortem", "C1", "111.1") is None


def test_게시가_실패하면_행을_남긴다(원장, 시각) -> None:
    """지워 놓고 못 알리면 그 점검은 영영 아무도 모른다."""
    원장.begin("postmortem", "C1", "111.1", by="U1")
    시각["값"] = 1200.0
    보고기 = _보고기(원장, _게시(실패=True))
    보고기.sweep()
    assert 원장.find("postmortem", "C1", "111.1") is not None


def test_도는_중인_점검은_안_알린다(원장, 시각) -> None:
    원장.begin("postmortem", "C1", "111.1", by="U1")
    시각["값"] = 1050.0
    게시 = _게시()
    _보고기(원장, 게시).sweep()
    assert 게시.보낸것 == []


def test_보고_채널이_비면_아무것도_안_한다(원장, 시각) -> None:
    """채널 미설정은 유효한 설정이다. 행을 지우면 안 된다."""
    원장.begin("postmortem", "C1", "111.1", by="U1")
    시각["값"] = 1200.0
    게시 = _게시()
    _보고기(원장, 게시, 채널="").sweep()
    assert 게시.보낸것 == []
    assert 원장.find("postmortem", "C1", "111.1") is not None


def test_게시가_예외를_내도_순회가_안_멈춘다(원장, 시각) -> None:
    """한 건이 막으면 나머지 중단 건도 영영 안 알려진다."""

    class _터짐(_게시):
        def post(self, channel, thread_ts, text, *, rich):  # type: ignore[no-untyped-def]
            if "111.1" in text:
                raise RuntimeError("슬랙 오류")
            return super().post(channel, thread_ts, text, rich=rich)

    원장.begin("postmortem", "C1", "111.1", by="U1")
    원장.begin("postmortem", "C1", "222.2", by="U2")
    시각["값"] = 1200.0
    게시 = _터짐()
    _보고기(원장, 게시).sweep()
    assert [t[2] for t in 게시.보낸것 if "222.2" in t[2]]
    assert 원장.find("postmortem", "C1", "111.1") is not None
    assert 원장.find("postmortem", "C1", "222.2") is None
