"""review/ledger.py 특성화 테스트.

원본 bot.py 의 load_postmortems/save_postmortem/drop_postmortem,
load_debug_traces/save_debug_trace/drop_debug_trace,
load_format_reviews/save_format_review/drop_format_review 세 벌을 하나의
`reviews` 테이블(kind, channel, target_ts, at, result)로 합친 ReviewLedger 를
검증한다. kind 로 세 점검 종류를 가른다.
"""

from __future__ import annotations

import pytest
from review_support import recorded

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


class Test중단된건의재시도:
    """진행 중으로 남은 건은 시간이 지나면 다시 붙일 수 있어야 한다.

    점검은 엔진을 부르는 동안 몇 분이 걸린다. 그 사이 프로세스가 재기동되면
    `complete` 도 `drop` 도 못 하고 "진행" 행만 남는다. 그 행을 완료와 같이
    보면 경단을 다시 붙여도 아무 일이 안 일어나고, 그 상태가 영구히 간다 --
    2026-09-16 에 레이에서 실제로 났다.
    """

    def test_진행중인건은아직막는다(self, ledger: ReviewLedger) -> None:
        """정말 돌고 있는 점검을 두 번 돌리면 답이 두 벌 올라간다."""
        ledger.begin("postmortem", "C1", "111.1", by="U1")
        ledger._시각["값"] += 60.0
        assert ledger.is_reviewed("postmortem", "C1", "111.1") is True

    def test_오래된진행은다시할수있다(self, ledger: ReviewLedger) -> None:
        ledger.begin("postmortem", "C1", "111.1", by="U1")
        ledger._시각["값"] += ReviewLedger.DEFAULT_STALE_SEC + 1
        assert ledger.is_reviewed("postmortem", "C1", "111.1") is False

    def test_완료는시간이지나도막는다(self, ledger: ReviewLedger) -> None:
        """이미 답을 낸 건이다. 오래됐다고 다시 부검하면 같은 보고가 또 올라간다."""
        ledger.complete("postmortem", "C1", "111.1", by="U1", link="L", report="R")
        ledger._시각["값"] += ReviewLedger.DEFAULT_STALE_SEC + 1
        assert ledger.is_reviewed("postmortem", "C1", "111.1") is True

    def test_기한은조립할때정한다(self, database) -> None:
        """엔진 제한시간이 봇마다 달라 고정값으로 둘 수 없다."""
        시각 = {"값": 1000.0}
        led = ReviewLedger(database, now=lambda: 시각["값"], stale_after_sec=10.0)
        led.begin("postmortem", "C1", "111.1", by="U1")
        시각["값"] += 11.0
        assert led.is_reviewed("postmortem", "C1", "111.1") is False


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
        rec = recorded(ledger, "postmortem", "C1", "111.1")
        assert rec == ReviewRecord(status="진행", by="U1")

    def test_완료상태를읽는다(self, ledger: ReviewLedger) -> None:
        ledger.complete("format_review", "C1", "111.1", by="U2", link="L1", report="R1")
        rec = recorded(ledger, "format_review", "C1", "111.1")
        assert rec == ReviewRecord(status="완료", by="U2", link="L1", report="R1")

    def test_완료가시작을덮는다(self, ledger: ReviewLedger) -> None:
        """같은 (kind, channel, target_ts) 는 최신 상태 하나만 남는다."""
        ledger.begin("postmortem", "C1", "111.1", by="U1")
        ledger.complete("postmortem", "C1", "111.1", by="U1", link="L", report="R")
        rec = recorded(ledger, "postmortem", "C1", "111.1")
        assert rec.status == "완료"

    def test_깨진값이면빈상태로본다(self, database, ledger: ReviewLedger) -> None:
        """사람이 손댔거나 옛 판이 남긴 값이 JSON 이 아닐 수 있다."""
        ledger.begin("postmortem", "C1", "111.1", by="U1")
        database.connect().execute(
            "UPDATE reviews SET result = '{망가짐' WHERE kind='postmortem'"
        )
        rec = recorded(ledger, "postmortem", "C1", "111.1")
        assert rec == ReviewRecord(status="")


class Test중단된_점검_찾기:
    """프로세스가 재기동되면 도는 점검이 죽고 진행 행만 남는다(sca-9bq).

    24cccf2 로 그 행이 재시도를 영구히 막던 것은 고쳤지만, 사용자는 여전히
    죽은 것을 모르고 기다린다. 알리려면 먼저 찾을 수 있어야 한다.
    """

    def _원장(self, database, 시각: dict) -> ReviewLedger:
        return ReviewLedger(database, now=lambda: 시각["값"], stale_after_sec=100.0)

    def test_기한을_넘긴_진행_행을_돌려준다(self, database) -> None:
        시각 = {"값": 1000.0}
        led = self._원장(database, 시각)
        led.begin("postmortem", "C1", "111.1", by="U1")
        시각["값"] = 1101.0
        중단 = led.stale_in_progress()
        assert [(s.kind, s.channel, s.target_ts, s.record.by) for s in 중단] == [
            ("postmortem", "C1", "111.1", "U1")
        ]

    def test_아직_기한_안인_진행_행은_빼놓는다(self, database) -> None:
        """도는 중인 점검을 중단으로 알리면 거짓 경보가 된다."""
        시각 = {"값": 1000.0}
        led = self._원장(database, 시각)
        led.begin("postmortem", "C1", "111.1", by="U1")
        시각["값"] = 1099.0
        assert led.stale_in_progress() == []

    def test_완료_행은_아무리_오래돼도_안_나온다(self, database) -> None:
        시각 = {"값": 1000.0}
        led = self._원장(database, 시각)
        led.complete("postmortem", "C1", "111.1", by="U1")
        시각["값"] = 99999.0
        assert led.stale_in_progress() == []

    def test_중단_시각을_함께_돌려준다(self, database) -> None:
        """알림에 언제부터 멈춰 있었는지를 적으려면 필요하다."""
        시각 = {"값": 1000.0}
        led = self._원장(database, 시각)
        led.begin("debug_trace", "C1", "111.1", by="U1")
        시각["값"] = 1200.0
        assert led.stale_in_progress()[0].at == 1000.0
