"""검수 원장을 읽는 시험용 도우미."""

from __future__ import annotations

from slack_cli_agent.review.ledger import ReviewLedger, ReviewRecord


def recorded(ledger: ReviewLedger, kind: str, channel: str, target_ts: str) -> ReviewRecord:
    """find 가 None 을 내면 그 자리에서 실패시킨다."""
    record = ledger.find(kind, channel, target_ts)
    assert record is not None
    return record


def header_body_lines(header: str) -> list[str]:
    """머리말에서 제목과 구분선, 빈 줄을 뺀 나머지 줄."""
    return [
        line
        for line in header.splitlines()
        if line.strip() and not line.startswith("# ") and line.strip() != "---"
    ]


def assert_header_is_one_table(header: str) -> None:
    """머리말의 값이 표 한 벌로만 나오는지 본다.

    요청자 줄이 표 뒤에 평문으로 덧붙어 같은 머리말에서 같은 종류의 값이
    두 형태로 갈렸다. 그 형태가 다시 들어오면 여기서 실패한다.
    """
    lines = header_body_lines(header)
    assert lines, "머리말에 표가 없다"
    assert all(line.startswith("|") for line in lines), f"표 밖에 있는 줄이 있다 : {lines}"
