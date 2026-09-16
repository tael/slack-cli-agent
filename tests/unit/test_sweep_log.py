"""정리 작업의 실행 흔적(sca-mf6).

job_purge 와 첨부 정리는 지운 것이 있을 때만 로그를 남겼다. 그래서 기동 로그에
이름만 나오고 그 뒤로 조용한 상태가, 지울 것이 없어서인지 아예 안 도는지
구분되지 않았다.
"""

from __future__ import annotations

import logging

import pytest

from slack_cli_agent.core.sweeplog import SweepLog


class TestSweepLog:
    def test_지운_것이_있으면_건수를_남긴다(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            SweepLog("끝난 작업").record(3)
        assert any("끝난 작업" in r.getMessage() and "3" in r.getMessage() for r in caplog.records)

    def test_0건도_남긴다(self, caplog: pytest.LogCaptureFixture) -> None:
        """이것이 없으면 조용한 것이 정상인지 고장인지 갈리지 않는다."""
        with caplog.at_level(logging.INFO):
            SweepLog("첨부").record(0)
        assert any("첨부" in r.getMessage() and "0" in r.getMessage() for r in caplog.records)

    def test_연속_0건이면_몇_회째인지_함께_남긴다(self, caplog: pytest.LogCaptureFixture) -> None:
        """같은 줄만 반복되면 마지막 실행 시각을 로그에서 못 읽는다."""
        sweep = SweepLog("첨부")
        with caplog.at_level(logging.INFO):
            sweep.record(0)
            sweep.record(0)
            sweep.record(0)
        메시지 = [r.getMessage() for r in caplog.records]
        assert len(메시지) == 3
        assert "3회" in 메시지[-1]

    def test_지운_뒤에는_연속_횟수가_다시_센다(self, caplog: pytest.LogCaptureFixture) -> None:
        sweep = SweepLog("첨부")
        sweep.record(0)
        sweep.record(0)
        sweep.record(5)
        with caplog.at_level(logging.INFO):
            sweep.record(0)
        assert "1회" in caplog.records[-1].getMessage()

    def test_누적_삭제_수를_함께_남긴다(self, caplog: pytest.LogCaptureFixture) -> None:
        """한 줄만 보고도 이 프로세스가 사는 동안 실제로 지운 것이 있는지 갈린다."""
        sweep = SweepLog("끝난 작업")
        sweep.record(2)
        with caplog.at_level(logging.INFO):
            sweep.record(3)
        assert "누적 5" in caplog.records[-1].getMessage()
