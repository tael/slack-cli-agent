"""감시 보고를 올리기 전 두 관문 시험 (C6).

- 확인 턴 기보고 판정 : 등록 뒤 스레드에서 이미 결과를 보고했으면 확인 턴이
  `WATCH_SUPERSEDED_TAG` 만 돌려주고, 체커는 보고 없이 감시를 닫는다
  (bot.py `_watch_check_prompt` 의 transcript, `job_watch` 의 stage=check).
- 보고 직전 물음 삭제 확인 : 등록과 보고 사이가 길어 그동안 물음이 지워지면
  보고가 남의 스레드에 남는다. 확실히 지워졌을 때(False)만 보고를 버린다
  (bot.py `job_watch` 의 origin_alive).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from slack_cli_agent.guard.watch import WATCH_DONE_TAG, WATCH_STILL_TAG, WATCH_SUPERSEDED_TAG
from slack_cli_agent.observability.audit import WATCH_FINISHED_KIND, IncidentKind
from slack_cli_agent.reliability.watchresult import WatchOutcome
from slack_cli_agent.reliability.watchrunner import WatchJobChecker, watch_check_prompt
from slack_cli_agent.storage.database import Database
from tests.unit.test_watchrunner import (
    가짜감사,
    가짜리액션,
    가짜발행,
    가짜채널목록,
    설정,
    시계달린큐,
    응답,
)


@pytest.fixture
def 큐(database: Database) -> 시계달린큐:
    return 시계달린큐(database)


@dataclass
class 가짜생존확인:
    값: bool | None = True
    터짐: bool = False
    받은인자: list[tuple[str, str]] = field(default_factory=list)

    def alive(self, channel: str, ts: str) -> bool | None:
        self.받은인자.append((channel, ts))
        if self.터짐:
            raise RuntimeError("조회 실패")
        return self.값


@dataclass
class 고정판정기:
    결과: WatchOutcome

    def read(self, run_id: str) -> WatchOutcome:
        return self.결과


def 체커(큐, run_check, *, 발행=None, 리액션=None, 감사=None, 생존=None, 판정기=None):
    return WatchJobChecker(
        queue=큐, run_check=run_check, results=판정기,
        publisher=발행 or 가짜발행(), channels=가짜채널목록(), settings=설정(),
        reactions=리액션, audit=감사, origin_alive=생존,
        now=lambda: 큐.시각["값"],
    )


class Test확인턴프롬프트:
    def test_대화록이_있으면_기보고_판정을_묻는다(self) -> None:
        prompt = watch_check_prompt("배포 확인", transcript="마메치: 배포 끝났습니다")
        assert "마메치: 배포 끝났습니다" in prompt
        assert WATCH_SUPERSEDED_TAG in prompt

    def test_결과가_정해진_보고_턴에도_대화록을_붙인다(self) -> None:
        prompt = watch_check_prompt("배포 확인", WatchOutcome.SUCCEEDED, transcript="이미 보고함")
        assert "이미 보고함" in prompt
        assert WATCH_SUPERSEDED_TAG in prompt

    def test_대화록이_없으면_기보고_안내를_넣지_않는다(self) -> None:
        assert WATCH_SUPERSEDED_TAG not in watch_check_prompt("배포 확인")


class Test확인턴기보고:
    def test_기보고_응답이면_보고없이_닫는다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인", msg_ts="222.2")
        발행, 리액션, 감사 = 가짜발행(), 가짜리액션(), 가짜감사()
        c = 체커(큐, lambda job, outcome: 응답(ok=True, body=WATCH_SUPERSEDED_TAG),
                발행=발행, 리액션=리액션, 감사=감사)
        큐.시각["값"] = 2000.0
        c.check_once()

        assert 발행.게시내역 == []
        assert 큐.due(now=99999.0, min_gap=0.0) == []
        assert 리액션.완료내역 == [("C1", "222.2")]
        기록 = dict(감사.기록)
        assert 기록[WATCH_FINISHED_KIND]["superseded"] is True
        assert 기록[WATCH_FINISHED_KIND]["stage"] == "check"

    def test_결과가_정해진_건도_기보고면_재보고하지_않는다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인")
        발행 = 가짜발행()
        c = 체커(큐, lambda job, outcome: 응답(ok=True, body=WATCH_SUPERSEDED_TAG),
                발행=발행, 판정기=고정판정기(WatchOutcome.SUCCEEDED))
        큐.시각["값"] = 2000.0
        c.check_once()

        assert 발행.게시내역 == []
        assert 큐.due(now=99999.0, min_gap=0.0) == []

    def test_엔진_실패면_기보고로_치지_않고_다시_확인한다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인")
        c = 체커(큐, lambda job, outcome: 응답(ok=False, body=WATCH_SUPERSEDED_TAG))
        큐.시각["값"] = 2000.0
        c.check_once()

        assert len(큐.due(now=99999.0, min_gap=0.0)) == 1

    def test_진행중_태그와_함께면_기보고로_치지_않는다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인")
        c = 체커(큐, lambda job, outcome: 응답(
            ok=True, body=f"{WATCH_SUPERSEDED_TAG} {WATCH_STILL_TAG}"))
        큐.시각["값"] = 2000.0
        c.check_once()

        assert len(큐.due(now=99999.0, min_gap=0.0)) == 1


class Test보고직전물음삭제:
    def test_물음이_지워졌으면_보고하지_않고_닫는다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인", msg_ts="222.2")
        발행, 리액션, 감사, 생존 = 가짜발행(), 가짜리액션(), 가짜감사(), 가짜생존확인(값=False)
        c = 체커(큐, lambda job, outcome: 응답(ok=True, body=f"배포됐습니다 {WATCH_DONE_TAG}"),
                발행=발행, 리액션=리액션, 감사=감사, 생존=생존)
        큐.시각["값"] = 2000.0
        c.check_once()

        assert 생존.받은인자 == [("C1", "222.2")]
        assert 발행.게시내역 == []
        assert 큐.due(now=99999.0, min_gap=0.0) == []
        assert 큐.pending_reports() == []
        # 지워진 메시지에는 표식을 달 수 없다.
        assert 리액션.완료내역 == []
        kinds = [k for k, _ in 감사.기록]
        assert IncidentKind.ORIGIN_DELETED.value in kinds

    def test_생존_확인을_모르면_보고한다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인", msg_ts="222.2")
        발행 = 가짜발행()
        c = 체커(큐, lambda job, outcome: 응답(ok=True, body=f"끝 {WATCH_DONE_TAG}"),
                발행=발행, 생존=가짜생존확인(값=None))
        큐.시각["값"] = 2000.0
        c.check_once()

        assert [t for _, _, t, _ in 발행.게시내역] == ["끝"]

    def test_생존_확인이_예외를_내도_보고한다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인", msg_ts="222.2")
        발행 = 가짜발행()
        c = 체커(큐, lambda job, outcome: 응답(ok=True, body=f"끝 {WATCH_DONE_TAG}"),
                발행=발행, 생존=가짜생존확인(터짐=True))
        큐.시각["값"] = 2000.0
        c.check_once()

        assert len(발행.게시내역) == 1

    def test_msg_ts가_없으면_생존을_묻지_않는다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인")
        생존 = 가짜생존확인(값=False)
        발행 = 가짜발행()
        c = 체커(큐, lambda job, outcome: 응답(ok=True, body=f"끝 {WATCH_DONE_TAG}"),
                발행=발행, 생존=생존)
        큐.시각["값"] = 2000.0
        c.check_once()

        assert 생존.받은인자 == []
        assert len(발행.게시내역) == 1


class Test조립:
    """Application 이 두 관문을 실제로 연결하는지 본다."""

    def test_확인턴_프롬프트에_등록뒤_대화록이_들어간다(self, tmp_path, monkeypatch) -> None:
        from slack_cli_agent.core.application import Application
        from slack_cli_agent.engine.runner import EngineRunner
        from slack_cli_agent.reliability.watchjobs import WatchJob
        from tests.unit.test_application import FakeSlackClient, _프롬프트조립대역, write_profile

        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        app._composer = lambda: _프롬프트조립대역()  # type: ignore[method-assign]
        받은인자: list[tuple[str, str, float]] = []

        class 대화록대역:
            def transcript_since(self, channel: str, thread_ts: str, since: float) -> str:
                받은인자.append((channel, thread_ts, since))
                return "마메치: 배포 끝났습니다"

        app.thread_activity = lambda: 대화록대역()  # type: ignore[method-assign,assignment,return-value]
        보낸요청: list = []
        monkeypatch.setattr(EngineRunner, "run", lambda self, engine, request: 보낸요청.append(request))

        app._watch_run_check(WatchJob(
            id=1, channel="C1", thread_ts="1.1", condition="배포 확인",
            created_at=50.0, last_run=None,
        ), WatchOutcome.UNKNOWN)

        assert 받은인자 == [("C1", "1.1", 50.0)]
        assert "마메치: 배포 끝났습니다" in 보낸요청[0].prompt

    def test_대화록_조회가_실패해도_확인은_돈다(self, tmp_path, monkeypatch) -> None:
        from slack_cli_agent.core.application import Application
        from slack_cli_agent.engine.runner import EngineRunner
        from slack_cli_agent.reliability.watchjobs import WatchJob
        from tests.unit.test_application import FakeSlackClient, _프롬프트조립대역, write_profile

        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        app._composer = lambda: _프롬프트조립대역()  # type: ignore[method-assign]

        class 터지는대역:
            def transcript_since(self, channel: str, thread_ts: str, since: float) -> str:
                raise RuntimeError("조회 실패")

        app.thread_activity = lambda: 터지는대역()  # type: ignore[method-assign,assignment,return-value]
        보낸요청: list = []
        monkeypatch.setattr(EngineRunner, "run", lambda self, engine, request: 보낸요청.append(request))

        app._watch_run_check(WatchJob(
            id=1, channel="C1", thread_ts="1.1", condition="배포 확인",
            created_at=50.0, last_run=None,
        ), WatchOutcome.UNKNOWN)

        assert len(보낸요청) == 1
        assert WATCH_SUPERSEDED_TAG not in 보낸요청[0].prompt

    def test_감시_체커에_생존확인이_연결된다(self, tmp_path) -> None:
        from slack_cli_agent.core.application import Application
        from slack_cli_agent.slack.origin_alive import SlackOriginAliveChecker
        from tests.unit.test_application import FakeSlackClient, write_profile

        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        assert isinstance(app.watch_checker()._origin_alive, SlackOriginAliveChecker)
