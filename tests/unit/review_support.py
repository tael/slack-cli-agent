"""검수 원장을 읽는 시험용 도우미."""

from __future__ import annotations

from slack_cli_agent.review.ledger import ReviewLedger, ReviewRecord


def recorded(ledger: ReviewLedger, kind: str, channel: str, target_ts: str) -> ReviewRecord:
    """find 가 None 을 내면 그 자리에서 실패시킨다."""
    record = ledger.find(kind, channel, target_ts)
    assert record is not None
    return record
