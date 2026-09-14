"""review/ledger.py 특성화 테스트.

원본 bot.py 의 load_postmortems/save_postmortem/drop_postmortem,
load_debug_traces/save_debug_trace/drop_debug_trace,
load_format_reviews/save_format_review/drop_format_review 세 벌을 하나의
`reviews` 테이블(kind, channel, target_ts, at, result)로 합친 ReviewLedger 를
검증한다. kind 로 세 점검 종류를 가른다.
"""

from __future__ import annotations

import pytest

from slack_cli_agent.review.ledger import ReviewLedger, ReviewRecord


@pytest.fixture
def ledger(database) -> ReviewLedger:
    시각 = {"값": 1000.0}
    led = ReviewLedger(database, now=lambda: 시각["값"])
    led._시각 = 시각
    return led


class Test중복방지:
    def test_등록전에는점검안된것이다(self, ledger: ReviewLedger) -> None:
        assert ledger.is_reviewed("postmortem", "C1", "111.1") is False

    def test_시작하면점검한것으로본다(self, ledger: ReviewLedger) -> None:
        ledger.begin("postmortem", "C1", "111.1", by="U1")
        assert ledger.is_reviewed("postmortem", "C1", "111.1") is True

    def test_완료해도점검한것이다(self, ledger: ReviewLedger) -> None:
        ledger.complete("postmortem", "C1", "111.1", by="U1", link="L", report="R")
        assert ledger.is_reviewed("postmortem", "C1", "111.1") is True

    def test_종류가다르면같은대상도별개다(self, ledger: ReviewLedger) -> None:
        """부검을 했어도 서식 점검은 아직 안 한 것이다."""
        ledger.complete("postmortem", "C1", "111.1", by="U1")
        assert ledger.is_reviewed("format_review", "C1", "111.1") is False

    def test_채널이다르면같은ts도별개다(self, ledger: ReviewLedger) -> None:
        ledger.complete("postmortem", "C1", "111.1", by="U1")
        assert ledger.is_reviewed("postmortem", "C2", "111.1") is False


class Test재시도:
    def test_지우면다시점검안된것이된다(self, ledger: ReviewLedger) -> None:
        """중단된 건은 리액션을 다시 붙이면 재시도할 수 있어야 한다."""
        ledger.begin("postmortem", "C1", "111.1", by="U1")
        ledger.drop("postmortem", "C1", "111.1")
        assert ledger.is_reviewed("postmortem", "C1", "111.1") is False

    def test_없는것을지워도예외가안난다(self, ledger: ReviewLedger) -> None:
        ledger.drop("postmortem", "C1", "111.1")


class Test조회:
    def test_등록전에는None이다(self, ledger: ReviewLedger) -> None:
        assert ledger.find("postmortem", "C1", "111.1") is None

    def test_시작상태를읽는다(self, ledger: ReviewLedger) -> None:
        ledger.begin("postmortem", "C1", "111.1", by="U1")
        rec = ledger.find("postmortem", "C1", "111.1")
        assert rec == ReviewRecord(status="진행", by="U1")

    def test_완료상태를읽는다(self, ledger: ReviewLedger) -> None:
        ledger.complete("format_review", "C1", "111.1", by="U2", link="L1", report="R1")
        rec = ledger.find("format_review", "C1", "111.1")
        assert rec == ReviewRecord(status="완료", by="U2", link="L1", report="R1")

    def test_완료가시작을덮는다(self, ledger: ReviewLedger) -> None:
        """같은 (kind, channel, target_ts) 는 최신 상태 하나만 남는다."""
        ledger.begin("postmortem", "C1", "111.1", by="U1")
        ledger.complete("postmortem", "C1", "111.1", by="U1", link="L", report="R")
        rec = ledger.find("postmortem", "C1", "111.1")
        assert rec.status == "완료"

    def test_깨진값이면빈상태로본다(self, database, ledger: ReviewLedger) -> None:
        """사람이 손댔거나 옛 판이 남긴 값이 JSON 이 아닐 수 있다."""
        ledger.begin("postmortem", "C1", "111.1", by="U1")
        database.connect().execute(
            "UPDATE reviews SET result = '{망가짐' WHERE kind='postmortem'"
        )
        rec = ledger.find("postmortem", "C1", "111.1")
        assert rec == ReviewRecord(status="")
