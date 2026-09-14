"""PendingReportStore — 보내지 못한 보고를 남겼다가 다시 보낸다.

재기동 순간은 슬랙 연결 자체가 불안정해 보고 발송이 실패하기 쉽다. 원본
`bot.py` 의 `save_pending_report`/`flush_pending_report`(단일 파일에 최신
보고 하나만 남기고, 성공했을 때만 지운다)를 클래스로 재구성했다. 원본
동작은 `bot.py:6570`, `bot.py:6687` 을 실행 없이 코드로 확인해 기대값을
정했다 — 파일 입출력만 하는 순수 로직이라 하네스로 돌려 볼 필요가 없었다.
"""

from __future__ import annotations

import json
from pathlib import Path


class TestPendingReportStore:
    def test_저장할_보고가_없으면_flush는_아무_일도_하지_않는다(self, tmp_path: Path) -> None:
        from slack_cli_agent.reliability.pending_report import PendingReportStore

        보낸_것: list[str] = []
        store = PendingReportStore(path=tmp_path / "pending_report.json", sender=보낸_것.append)

        store.flush()

        assert 보낸_것 == []

    def test_저장하면_파일에_JSON으로_남는다(self, tmp_path: Path) -> None:
        from slack_cli_agent.reliability.pending_report import PendingReportStore

        path = tmp_path / "pending_report.json"
        store = PendingReportStore(path=path, sender=lambda text: None)

        store.save("자동 재기동")

        저장된_내용 = json.loads(path.read_text())
        assert 저장된_내용["text"] == "자동 재기동"

    def test_저장_시각을_주입한_시계로_기록한다(self, tmp_path: Path) -> None:
        from slack_cli_agent.reliability.pending_report import PendingReportStore

        path = tmp_path / "pending_report.json"
        store = PendingReportStore(path=path, sender=lambda text: None, now=lambda: 1234.5)

        store.save("사유")

        저장된_내용 = json.loads(path.read_text())
        assert 저장된_내용["at"] == 1234.5

    def test_상위_디렉터리가_없으면_만든다(self, tmp_path: Path) -> None:
        from slack_cli_agent.reliability.pending_report import PendingReportStore

        path = tmp_path / "없던" / "디렉터리" / "pending_report.json"
        store = PendingReportStore(path=path, sender=lambda text: None)

        store.save("사유")

        assert path.exists()

    def test_flush가_성공하면_저장한_문구로_발송하고_파일을_지운다(self, tmp_path: Path) -> None:
        from slack_cli_agent.reliability.pending_report import PendingReportStore

        path = tmp_path / "pending_report.json"
        받은_문구: list[str] = []
        store = PendingReportStore(path=path, sender=받은_문구.append)
        store.save("자동 재기동 사유")

        store.flush()

        assert 받은_문구 == ["자동 재기동 사유"]
        assert not path.exists()

    def test_발송이_실패하면_저장_파일이_그대로_남는다(self, tmp_path: Path) -> None:
        """실패했는데 지우면 그 보고가 유실된다 — 이 모듈이 존재하는 이유다."""
        from slack_cli_agent.reliability.pending_report import PendingReportStore

        def 항상_실패(text: str) -> None:
            raise RuntimeError("슬랙에 아직 안 닿는다")

        path = tmp_path / "pending_report.json"
        store = PendingReportStore(path=path, sender=항상_실패)
        store.save("자동 재기동 사유")

        store.flush()

        assert path.exists()
        저장된_내용 = json.loads(path.read_text())
        assert 저장된_내용["text"] == "자동 재기동 사유"

    def test_나중_저장이_이전_저장을_덮어쓴다(self, tmp_path: Path) -> None:
        """단일 슬롯이다 — 원본이 write_text 로 매번 통째로 덮어쓰는 것과 같다."""
        from slack_cli_agent.reliability.pending_report import PendingReportStore

        path = tmp_path / "pending_report.json"
        받은_문구: list[str] = []
        store = PendingReportStore(path=path, sender=받은_문구.append)
        store.save("첫 번째 사유")
        store.save("두 번째 사유")

        store.flush()

        assert 받은_문구 == ["두 번째 사유"]

    def test_flush를_두_번_부르면_두_번째는_아무_일도_안_한다(self, tmp_path: Path) -> None:
        from slack_cli_agent.reliability.pending_report import PendingReportStore

        path = tmp_path / "pending_report.json"
        받은_문구: list[str] = []
        store = PendingReportStore(path=path, sender=받은_문구.append)
        store.save("사유")

        store.flush()
        store.flush()

        assert 받은_문구 == ["사유"]

    def test_저장_파일이_깨진_JSON이면_예외없이_넘어간다(self, tmp_path: Path) -> None:
        from slack_cli_agent.reliability.pending_report import PendingReportStore

        path = tmp_path / "pending_report.json"
        path.write_text("{ 이것은 JSON 이 아니다")
        받은_문구: list[str] = []
        store = PendingReportStore(path=path, sender=받은_문구.append)

        store.flush()

        assert 받은_문구 == []
