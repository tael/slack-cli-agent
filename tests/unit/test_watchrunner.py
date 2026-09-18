"""감시 큐 실행 계층 시험.

`WatchJobChecker` 가 큐에서 확인 대상을 꺼내 `run_check` 로 넘기고, 그 응답에
따라 완료·포기·재확인을 가르는지를 검증한다. 엔진 실행 자체는 흉내 내지
않는다 — `run_check` 콜백이 그 자리를 대신한다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.engine.base import NO_DETAIL, EngineResponse, FailureDetail
from slack_cli_agent.guard.watch import WATCH_DONE_TAG, WATCH_STILL_TAG
from slack_cli_agent.observability.audit import (
    WATCH_ABANDONED_KIND,
    WATCH_CHECKED_KIND,
    WATCH_FINISHED_KIND,
)
from slack_cli_agent.reliability.watchjobs import WatchJob, WatchJobQueue
from slack_cli_agent.reliability.watchresult import WatchOutcome
from slack_cli_agent.reliability.watchrunner import WatchJobChecker
from slack_cli_agent.storage.database import Database


def 응답(
    *, ok: bool, body: str, failure_reason: str | None = None,
    failure_detail: FailureDetail = NO_DETAIL,
) -> EngineResponse:
    return EngineResponse(
        ok=ok, body=body, session_id=None, model_actual=None,
        elapsed=0.0, turns=None, usage=None, failure_reason=failure_reason,
        failure_detail=failure_detail,
    )


class 시계달린큐(WatchJobQueue):
    """시험이 시각을 앞으로 돌릴 수 있게 그 dict 를 큐에 달아 둔다.
    실물에 없는 속성이라 인스턴스에 덮어쓰면 타입 검사가 못 본다."""

    def __init__(self, database: Database) -> None:
        self.시각 = {"값": 1000.0}
        super().__init__(database, now=lambda: self.시각["값"])


@pytest.fixture
def 큐(database: Database) -> 시계달린큐:
    return 시계달린큐(database)


@dataclass
class 가짜채널설정:
    rich: bool = False


class 가짜채널목록:
    def __init__(self, configs: dict[str, 가짜채널설정] | None = None) -> None:
        self._configs = configs or {}

    def get(self, channel_id: str) -> 가짜채널설정 | None:
        return self._configs.get(channel_id)


@dataclass
class 가짜발행:
    실패: bool = False
    게시내역: list[tuple[str, str, str, bool]] = field(default_factory=list)

    def post(self, channel: str, thread_ts: str, text: str, rich: bool) -> str | None:
        if self.실패:
            raise RuntimeError("발신 실패")
        self.게시내역.append((channel, thread_ts, text, rich))
        return "999.9"


@dataclass
class 가짜리액션:
    제거내역: list[tuple[str, str, str]] = field(default_factory=list)
    완료내역: list[tuple[str, str]] = field(default_factory=list)
    실패내역: list[tuple[str, str]] = field(default_factory=list)

    def remove(self, channel: str, ts: str, name: str) -> None:
        self.제거내역.append((channel, ts, name))

    def mark_done(self, channel: str, ts: str) -> None:
        self.완료내역.append((channel, ts))

    def mark_failed(self, channel: str, ts: str) -> None:
        self.실패내역.append((channel, ts))


def 설정(**overrides: Any) -> RuntimeSettings:
    return RuntimeSettings().override(overrides)


def 기록하고_응답(
    기록: list[Any], 응답값: EngineResponse
) -> Callable[[WatchJob, WatchOutcome], EngineResponse]:
    def run(job: WatchJob, outcome: WatchOutcome) -> EngineResponse:
        기록.append(job)
        return 응답값

    return run


def 체커(*, 큐, run_check, 발행=None, 채널목록=None, 설정값=None, 리액션=None, notify_owner=None,
        판정기=None, 감사=None):
    return WatchJobChecker(
        queue=큐,
        run_check=run_check,
        results=판정기,
        publisher=발행 or 가짜발행(),
        channels=채널목록 or 가짜채널목록(),
        settings=설정값 or 설정(),
        reactions=리액션,
        notify_owner=notify_owner,
        audit=감사,
        now=lambda: 큐.시각["값"],
    )


class Test완료처리:
    def test_완료태그가있으면게시하고큐에서완료로표시한다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인", msg_ts="222.2")
        발행 = 가짜발행()
        c = 체커(
            큐=큐, run_check=lambda job, outcome: 응답(ok=True, body=f"배포됐습니다 {WATCH_DONE_TAG}"),
            발행=발행,
        )
        큐.시각["값"] = 2000.0
        c.check_once()

        assert 발행.게시내역 == [("C1", "111.1", "배포됐습니다", True)]
        assert 큐.due(now=99999.0, min_gap=0.0) == []
        assert 큐.expired(now=99999.0, max_age=0.0) == []

    def test_게시한본문에완료태그문자열이남지않는다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인")
        발행 = 가짜발행()
        c = 체커(
            큐=큐,
            run_check=lambda job, outcome: 응답(ok=True, body=f"결과 문구\n{WATCH_DONE_TAG}"),
            발행=발행,
        )
        큐.시각["값"] = 2000.0
        c.check_once()

        assert WATCH_DONE_TAG not in 발행.게시내역[0][2]

    def test_완료시완료표식하나로정리한다(self, 큐) -> None:
        """감시 표식을 여기서 따로 떼지 않는다. mark_done 이 부르는 _settle 이
        미완료 표식과 함께 감시 표식도 뗀다 (sca-3p6)."""
        큐.enqueue("C1", "111.1", "배포 확인", msg_ts="222.2")
        리액션 = 가짜리액션()
        c = 체커(
            큐=큐, run_check=lambda job, outcome: 응답(ok=True, body=f"끝{WATCH_DONE_TAG}"),
            리액션=리액션,
        )
        큐.시각["값"] = 2000.0
        c.check_once()

        assert 리액션.제거내역 == []
        assert 리액션.완료내역 == [("C1", "222.2")]

    def test_msg_ts가비어있으면리액션을건드리지않는다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인")  # msg_ts 기본값 ""
        리액션 = 가짜리액션()
        c = 체커(
            큐=큐, run_check=lambda job, outcome: 응답(ok=True, body=f"끝{WATCH_DONE_TAG}"),
            리액션=리액션,
        )
        큐.시각["값"] = 2000.0
        c.check_once()

        assert 리액션.제거내역 == []
        assert 리액션.완료내역 == []

    def test_리치채널이면게시가리치로나간다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인")
        발행 = 가짜발행()
        c = 체커(
            큐=큐, run_check=lambda job, outcome: 응답(ok=True, body=f"끝{WATCH_DONE_TAG}"),
            발행=발행, 채널목록=가짜채널목록({"C1": 가짜채널설정(rich=True)}),
        )
        큐.시각["값"] = 2000.0
        c.check_once()

        assert 발행.게시내역[0][3] is True

    def test_게시가예외를내도완료표시는된다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인", msg_ts="222.2")
        발행 = 가짜발행(실패=True)
        리액션 = 가짜리액션()
        c = 체커(
            큐=큐, run_check=lambda job, outcome: 응답(ok=True, body=f"끝{WATCH_DONE_TAG}"),
            발행=발행, 리액션=리액션,
        )
        큐.시각["값"] = 2000.0
        c.check_once()

        assert 리액션.완료내역 == [("C1", "222.2")]
        assert 큐.due(now=99999.0, min_gap=0.0) == []


class Test빈완료보고:
    """완료 태그만 오고 본문이 없을 때 스레드에 무엇이 올라가는가.

    2026-09-16 실측 — 확인 응답이 태그뿐이라 본문이 비었고, rich 채널의 발행기가
    보낼 조각을 하나도 못 만들어 아무것도 안 올라갔다. 그런데 완료 표식은 달려서
    재시도도 안 됐다. 사람이 보기에는 감시가 조용히 사라진 것과 같다.
    """

    def test_본문이_태그뿐이면_기본_문구로_올린다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "민감할 수 있는 감시 조건")
        발행 = 가짜발행()
        c = 체커(큐=큐, run_check=lambda job, outcome: 응답(ok=True, body=WATCH_DONE_TAG), 발행=발행)
        큐.시각["값"] = 2000.0
        c.check_once()

        assert len(발행.게시내역) == 1
        본문 = 발행.게시내역[0][2]
        assert 본문.strip()
        # 감시 조건에는 링크된 스레드나 파일에서 끌어온 내용이 들어갈 수 있다.
        # 이 문구는 채널 스레드로 나가므로 조건을 싣지 않는다.
        assert "민감할 수 있는 감시 조건" not in 본문

    def test_공백만_있어도_기본_문구로_올린다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인")
        발행 = 가짜발행()
        c = 체커(큐=큐, run_check=lambda job, outcome: 응답(ok=True, body=f"  \n {WATCH_DONE_TAG}\n "), 발행=발행)
        큐.시각["값"] = 2000.0
        c.check_once()

        assert 발행.게시내역[0][2].strip()

    def test_본문이_있으면_그대로_올린다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인")
        발행 = 가짜발행()
        c = 체커(큐=큐, run_check=lambda job, outcome: 응답(ok=True, body=f"끝났습니다 {WATCH_DONE_TAG}"), 발행=발행)
        큐.시각["값"] = 2000.0
        c.check_once()

        assert 발행.게시내역[0][2] == "끝났습니다"


class Test태그가둘다있을때:
    """완료 태그와 진행 태그가 한 응답에 같이 왔을 때.

    원본 bot.py:6788 도 완료 태그를 먼저 보고 완료로 처리한다. 판정은 원본을
    그대로 두되, 게시 본문에 내부 태그가 남는 것은 고친다 — 그 문자열은 사람이
    읽는 자리에 나갈 것이 아니다.
    """

    def test_두_태그가_다_지워진_본문이_올라간다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인")
        발행 = 가짜발행()
        c = 체커(
            큐=큐,
            run_check=lambda job, outcome: 응답(ok=True, body=f"{WATCH_STILL_TAG} 끝났습니다 {WATCH_DONE_TAG}"),
            발행=발행,
        )
        큐.시각["값"] = 2000.0
        c.check_once()

        본문 = 발행.게시내역[0][2]
        assert WATCH_DONE_TAG not in 본문
        assert WATCH_STILL_TAG not in 본문
        assert "끝났습니다" in 본문

    def test_모순된_응답이라는_것을_기록한다(self, 큐, caplog) -> None:
        큐.enqueue("C1", "111.1", "배포 확인")
        c = 체커(
            큐=큐,
            run_check=lambda job, outcome: 응답(ok=True, body=f"{WATCH_STILL_TAG}{WATCH_DONE_TAG}"),
        )
        큐.시각["값"] = 2000.0
        with caplog.at_level(logging.WARNING):
            c.check_once()
        기록 = [r.getMessage() for r in caplog.records]
        assert any("태그" in m and "모순" in m for m in 기록)
        # 완료 우선이라는 계약. 경고만 내고 미완료로 되돌리면 감시가 영영 안 끝난다.
        assert 큐.due(3000.0, 0.0) == []
        assert any("작업 1" in m and "확인 0회" in m for m in 기록)
        assert not any("배포 확인" in m for m in 기록)

    def test_완료_태그만_있으면_기록이_안_남는다(self, 큐, caplog) -> None:
        큐.enqueue("C1", "111.1", "배포 확인")
        c = 체커(큐=큐, run_check=lambda job, outcome: 응답(ok=True, body=f"끝 {WATCH_DONE_TAG}"))
        큐.시각["값"] = 2000.0
        with caplog.at_level(logging.WARNING):
            c.check_once()
        assert caplog.records == []


class Test미완료처리:
    def test_아직안끝났으면게시하지않고확인횟수만갱신한다(self, 큐) -> None:
        작업_id = 큐.enqueue("C1", "111.1", "배포 확인")
        발행 = 가짜발행()
        c = 체커(
            큐=큐, run_check=lambda job, outcome: 응답(ok=True, body=f"아직 {WATCH_STILL_TAG}"),
            발행=발행,
        )
        큐.시각["값"] = 2000.0
        c.check_once()

        assert 발행.게시내역 == []
        남은 = 큐.due(now=99999.0, min_gap=0.0)
        assert len(남은) == 1
        assert 남은[0].id == 작업_id
        assert 남은[0].checks == 1

    def test_엔진응답이실패해도예외없이확인횟수가갱신된다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인")
        c = 체커(큐=큐, run_check=lambda job, outcome: 응답(ok=False, body=""))
        큐.시각["값"] = 2000.0
        c.check_once()  # 예외가 안 나면 통과

        assert 큐.due(now=99999.0, min_gap=0.0)[0].checks == 1

    def test_run_check가예외를내도다음건의확인이계속된다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "실패할 건")
        큐.enqueue("C1", "111.2", "정상 확인될 건")

        def run_check(job: WatchJob, outcome: Any) -> EngineResponse:
            if job.condition == "실패할 건":
                raise RuntimeError("엔진 오류")
            return 응답(ok=True, body=f"아직 {WATCH_STILL_TAG}")

        c = 체커(큐=큐, run_check=run_check)
        큐.시각["값"] = 2000.0
        c.check_once()

        남은 = {작업.condition: 작업.checks for 작업 in 큐.due(now=99999.0, min_gap=0.0)}
        assert 남은 == {"실패할 건": 1, "정상 확인될 건": 1}


class Test게시실패기록:
    def test_완료_보고_발송_실패에_작업_식별자가_남는다(self, 큐, caplog) -> None:
        """식별자가 없으면 어느 감시 건이 실패했는지 로그만으로 못 찾는다.
        감시 조건 자체는 슬랙 대화나 파일 내용을 담을 수 있어 안 남긴다."""
        큐.enqueue("C1", "111.1", "민감할 수 있는 감시 조건")
        c = 체커(
            큐=큐,
            run_check=lambda job, outcome: 응답(ok=True, body=f"끝 {WATCH_DONE_TAG}"),
            발행=가짜발행(실패=True),
        )
        큐.시각["값"] = 2000.0
        with caplog.at_level(logging.ERROR):
            c.check_once()
        기록 = [r.getMessage() for r in caplog.records]
        assert any("작업 1" in m and "C1" in m for m in 기록)
        assert not any("민감할 수 있는 감시 조건" in m for m in 기록)


class Test확인실패진단:
    """확인이 실패했을 때 무엇이 로그에 남는가.

    실패가 엔진 오류인지 모델의 태그 누락인지 로그만으로 갈려야 한다. 그런데
    응답 본문과 감시 조건은 슬랙 대화·파일 내용이 그대로 들어올 수 있으므로
    로그에 남기지 않는다.
    """

    def test_엔진실패는_원인과_작업식별자를_남긴다(self, 큐, caplog) -> None:
        작업_id = 큐.enqueue("C1", "111.1", "배포 확인")
        c = 체커(큐=큐, run_check=lambda job, outcome: 응답(ok=False, body="", failure_reason="timeout"))
        큐.시각["값"] = 2000.0
        with caplog.at_level("WARNING"):
            c.check_once()

        기록 = "\n".join(r.getMessage() for r in caplog.records)
        assert "감시 확인 엔진 실패" in 기록
        assert "timeout" in 기록
        assert f"작업 {작업_id}" in 기록
        assert 큐.due(now=99999.0, min_gap=0.0)[0].checks == 1

    def test_엔진실패는_진단값도_남긴다(self, 큐, caplog) -> None:
        """사유만으로는 무엇이 잘못됐는지 모른다. 실제 원인이 raw 에만 있어
        명령을 손으로 재구성해야 했다(sca-dyb.14).
        """
        큐.enqueue("C1", "111.1", "배포 확인")
        c = 체커(큐=큐, run_check=lambda job, outcome: 응답(
            ok=False, body="", failure_reason="nonzero_exit", failure_detail=FailureDetail(exit_code=137)))
        큐.시각["값"] = 2000.0
        with caplog.at_level("WARNING"):
            c.check_once()

        assert "exit_code=137" in "\n".join(r.getMessage() for r in caplog.records)

    def test_태그누락은_엔진실패와_다른_문구로_남는다(self, 큐, caplog) -> None:
        큐.enqueue("C1", "111.1", "배포 확인")
        c = 체커(큐=큐, run_check=lambda job, outcome: 응답(ok=True, body="그냥 답했다"))
        큐.시각["값"] = 2000.0
        with caplog.at_level("WARNING"):
            c.check_once()

        기록 = "\n".join(r.getMessage() for r in caplog.records)
        assert "감시 확인 태그 누락" in 기록
        assert "감시 확인 엔진 실패" not in 기록

    def test_응답본문과_감시조건은_로그에_안_남는다(self, 큐, caplog) -> None:
        큐.enqueue("C1", "111.1", "xoxb-비밀토큰-조건")
        c = 체커(큐=큐, run_check=lambda job, outcome: 응답(ok=True, body="xoxb-비밀토큰-본문"))
        큐.시각["값"] = 2000.0
        with caplog.at_level("WARNING"):
            c.check_once()

        기록 = "\n".join(r.getMessage() for r in caplog.records)
        assert "비밀토큰" not in 기록

    def test_엔진실패_경로에서도_응답본문이_로그에_안_남는다(self, 큐, caplog) -> None:
        큐.enqueue("C1", "111.1", "배포 확인")
        c = 체커(
            큐=큐,
            run_check=lambda job, outcome: 응답(ok=False, body="xoxb-비밀토큰-본문", failure_reason="timeout"),
        )
        큐.시각["값"] = 2000.0
        with caplog.at_level("WARNING"):
            c.check_once()

        assert "비밀토큰" not in "\n".join(r.getMessage() for r in caplog.records)

    def test_정상완료와_진행중에는_경고가_안_남는다(self, 큐, caplog) -> None:
        큐.enqueue("C1", "111.1", "끝난 건")
        큐.enqueue("C1", "111.2", "진행중인 건")

        def run_check(job: WatchJob, outcome: Any) -> EngineResponse:
            태그 = WATCH_DONE_TAG if job.condition == "끝난 건" else WATCH_STILL_TAG
            return 응답(ok=True, body=f"보고 {태그}")

        c = 체커(큐=큐, run_check=run_check, 발행=가짜발행())
        큐.시각["값"] = 2000.0
        with caplog.at_level("WARNING"):
            c.check_once()

        assert [r.getMessage() for r in caplog.records] == []

    def test_run_check_예외는_작업식별자와_추적을_남긴다(self, 큐, caplog) -> None:
        작업_id = 큐.enqueue("C1", "111.1", "터지는 건")

        def run_check(job: WatchJob, outcome: Any) -> EngineResponse:
            raise RuntimeError("엔진 오류")

        c = 체커(큐=큐, run_check=run_check)
        큐.시각["값"] = 2000.0
        with caplog.at_level("ERROR"):
            c.check_once()

        assert any(f"작업 {작업_id}" in r.getMessage() for r in caplog.records)
        assert any(r.exc_info is not None for r in caplog.records)


class Test포기처리:
    def test_포기대상은소유자통지가나가고완료로표시된다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "오래된 건")
        통지내역: list[str] = []
        c = 체커(
            큐=큐,
            run_check=lambda job, outcome: 응답(ok=True, body=f"아직 {WATCH_STILL_TAG}"),
            notify_owner=통지내역.append,
            설정값=설정(watch_job_max_age_sec=500.0),
        )
        큐.시각["값"] = 2000.0  # 생성 후 1000초 경과, 상한 500초
        c.check_once()

        assert len(통지내역) == 1
        assert "오래된 건" in 통지내역[0]
        assert 큐.due(now=99999.0, min_gap=0.0) == []
        assert 큐.expired(now=99999.0, max_age=0.0) == []

    def test_notify_owner가없어도포기대상이완료로표시된다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "오래된 건")
        c = 체커(
            큐=큐, run_check=lambda job, outcome: 응답(ok=True, body=f"아직 {WATCH_STILL_TAG}"),
            설정값=설정(watch_job_max_age_sec=500.0),
        )
        큐.시각["값"] = 2000.0
        c.check_once()  # notify_owner 없이도 예외 없이 끝나야 한다

        assert 큐.due(now=99999.0, min_gap=0.0) == []
        assert 큐.expired(now=99999.0, max_age=0.0) == []

    def test_같은회차에서포기처리한건에는엔진을부르지않는다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "오래된 건")
        불린횟수 = {"값": 0}

        def run_check(job: WatchJob, outcome: Any) -> EngineResponse:
            불린횟수["값"] += 1
            return 응답(ok=True, body=f"아직 {WATCH_STILL_TAG}")

        c = 체커(
            큐=큐, run_check=run_check,
            설정값=설정(watch_job_max_age_sec=500.0, watch_job_min_gap_sec=0.0),
        )
        큐.시각["값"] = 2000.0
        c.check_once()

        assert 불린횟수["값"] == 0


class 고정판정기:
    """결과 파일을 만들지 않고 판정을 바로 말한다. 파일 배치를 세우면 그
    배치가 맞는지가 시험의 주제가 돼 버린다."""

    def __init__(self, outcome) -> None:
        self._outcome = outcome
        self.받은인자: list[tuple[str, str]] = []

    def read(self, workdir: str, run_id: str):
        self.받은인자.append((workdir, run_id))
        return self._outcome


class Test결과파일로_완료를_가른다:
    """지금까지는 모델이 완료 태그를 붙였는지만 봤다. 성공과 실패를 못 가르고,
    태그를 잘못 붙이면 그대로 완료가 됐다 (sca-17p).
    """

    def _호출기록(self):
        호출: list[Any] = []

        def run_check(job, outcome):
            호출.append((job, outcome))
            return 응답(ok=True, body="결과 문구")

        return 호출, run_check

    def test_진행_중이면_엔진을_안_부른다(self, 큐) -> None:
        """확인 한 번이 엔진 호출 한 번이다. 안 끝난 것을 물어볼 이유가 없다."""
        from slack_cli_agent.reliability.watchresult import WatchOutcome

        큐.enqueue("C1", "111.1", "배포 확인", workdir="/w", run_id="r1")
        호출, run_check = self._호출기록()
        c = 체커(큐=큐, run_check=run_check, 판정기=고정판정기(WatchOutcome.RUNNING))
        큐.시각["값"] = 2000.0
        c.check_once()

        assert 호출 == []
        # 확인 횟수는 엔진에 물어본 횟수다. 안 물어본 것을 세면 확인 상한이
        # 진행 중인 작업을 먼저 소진시킨다.
        assert [작업.checks for 작업 in 큐.due(now=99999.0, min_gap=0.0)] == [0]

    def test_성공으로_끝났으면_태그가_없어도_완료다(self, 큐) -> None:
        from slack_cli_agent.reliability.watchresult import WatchOutcome

        큐.enqueue("C1", "111.1", "배포 확인", workdir="/w", run_id="r1")
        발행 = 가짜발행()
        c = 체커(
            큐=큐, run_check=lambda job, outcome: 응답(ok=True, body="배포됐습니다"),
            발행=발행, 판정기=고정판정기(WatchOutcome.SUCCEEDED),
        )
        큐.시각["값"] = 2000.0
        c.check_once()

        assert 발행.게시내역 == [("C1", "111.1", "배포됐습니다", True)]
        assert 큐.due(now=99999.0, min_gap=0.0) == []

    def test_실패로_끝났어도_완료다(self, 큐) -> None:
        """실패도 끝난 것이다. 재확인을 계속하면 확인 상한까지 엔진을 부른다."""
        from slack_cli_agent.reliability.watchresult import WatchOutcome

        큐.enqueue("C1", "111.1", "배포 확인", workdir="/w", run_id="r1")
        발행 = 가짜발행()
        c = 체커(
            큐=큐, run_check=lambda job, outcome: 응답(ok=True, body="실패했습니다"),
            발행=발행, 판정기=고정판정기(WatchOutcome.FAILED),
        )
        큐.시각["값"] = 2000.0
        c.check_once()

        assert 발행.게시내역 == [("C1", "111.1", "실패했습니다", True)]
        assert 큐.due(now=99999.0, min_gap=0.0) == []

    def test_판정_결과를_확인턴에_넘긴다(self, 큐) -> None:
        """성공과 실패는 보고 문구가 다르다. 그 구분을 확인 턴이 알아야 한다."""
        from slack_cli_agent.reliability.watchresult import WatchOutcome

        큐.enqueue("C1", "111.1", "배포 확인", workdir="/w", run_id="r1")
        호출, run_check = self._호출기록()
        c = 체커(큐=큐, run_check=run_check, 판정기=고정판정기(WatchOutcome.FAILED))
        큐.시각["값"] = 2000.0
        c.check_once()

        assert [결과 for _작업, 결과 in 호출] == [WatchOutcome.FAILED]

    def test_판정_불가면_예전대로_태그로_가른다(self, 큐) -> None:
        """이 컬럼이 생기기 전에 등록된 건은 볼 파일이 없다."""
        from slack_cli_agent.reliability.watchresult import WatchOutcome

        큐.enqueue("C1", "111.1", "배포 확인")
        발행 = 가짜발행()
        c = 체커(
            큐=큐, run_check=lambda job, outcome: 응답(ok=True, body="아직입니다 " + WATCH_STILL_TAG),
            발행=발행, 판정기=고정판정기(WatchOutcome.UNKNOWN),
        )
        큐.시각["값"] = 2000.0
        c.check_once()

        assert 발행.게시내역 == []
        assert [작업.checks for 작업 in 큐.due(now=99999.0, min_gap=0.0)] == [1]

    def test_판정기가_없으면_예전대로_돈다(self, 큐) -> None:
        """판정기를 안 준 조립에서도 감시가 계속 돌아야 한다."""
        큐.enqueue("C1", "111.1", "배포 확인")
        발행 = 가짜발행()
        c = 체커(
            큐=큐,
            run_check=lambda job, outcome: 응답(ok=True, body=f"끝 {WATCH_DONE_TAG}"),
            발행=발행,
        )
        큐.시각["값"] = 2000.0
        c.check_once()

        assert 발행.게시내역 == [("C1", "111.1", "끝", True)]


class Test종료_상태가_포기보다_앞선다:
    """포기를 먼저 처리하면 이미 끝난 작업도 완료 보고 없이 사라진다. 종료
    상태를 읽었다면 그것이 시각보다 우선이다 (코덱스 검토).
    """

    def test_만료_시각이_지나도_끝난_건은_보고한다(self, 큐) -> None:
        from slack_cli_agent.reliability.watchresult import WatchOutcome

        큐.enqueue("C1", "111.1", "배포 확인", workdir="/w", run_id="r1")
        발행 = 가짜발행()
        통지: list[str] = []
        c = 체커(
            큐=큐, run_check=lambda job, outcome: 응답(ok=True, body="배포됐습니다"),
            발행=발행, 판정기=고정판정기(WatchOutcome.SUCCEEDED),
            설정값=설정(watch_job_max_age_sec=10.0), notify_owner=통지.append,
        )
        큐.시각["값"] = 99999.0
        c.check_once()

        assert 발행.게시내역 == [("C1", "111.1", "배포됐습니다", True)]
        assert 통지 == []

    def test_아직_안_끝난_건은_예전대로_포기한다(self, 큐) -> None:
        from slack_cli_agent.reliability.watchresult import WatchOutcome

        큐.enqueue("C1", "111.1", "배포 확인", workdir="/w", run_id="r1")
        통지: list[str] = []
        c = 체커(
            큐=큐, run_check=lambda job, outcome: 응답(ok=True, body="무시"),
            판정기=고정판정기(WatchOutcome.RUNNING),
            설정값=설정(watch_job_max_age_sec=10.0), notify_owner=통지.append,
        )
        큐.시각["값"] = 99999.0
        c.check_once()

        assert len(통지) == 1
        assert 큐.due(now=99999.0, min_gap=0.0) == []


class Test실패로_끝난_건의_기본_보고:
    """보고가 비었을 때 '끝났습니다' 만 쓰면 실패가 성공처럼 읽힌다."""

    def test_보고가_비면_실패였다고_쓴다(self, 큐) -> None:
        from slack_cli_agent.reliability.watchresult import WatchOutcome

        큐.enqueue("C1", "111.1", "배포 확인", workdir="/w", run_id="r1")
        발행 = 가짜발행()
        c = 체커(
            큐=큐, run_check=lambda job, outcome: 응답(ok=True, body="   "),
            발행=발행, 판정기=고정판정기(WatchOutcome.FAILED),
        )
        큐.시각["값"] = 2000.0
        c.check_once()

        본문 = 발행.게시내역[0][2]
        assert "실패" in 본문


class Test실제_결과_파일로_판정한다:
    """고정 판정기로만 보면 Reader 와 체커 사이의 인자 순서가 뒤집혀도 안
    걸린다. 파일을 실제로 두고 끝까지 돌린다 (코덱스 검토).
    """

    def test_표식이_찍힌_파일이_있으면_태그_없이_완료된다(self, 큐, tmp_path) -> None:
        from slack_cli_agent.reliability.watchresult import WatchResultReader

        결과 = tmp_path / ".watch-out"
        결과.mkdir()
        (결과 / "r1.out").write_text("다 됐다\n__SCA_WATCH_EXIT__=0\n", encoding="utf-8")
        큐.enqueue("C1", "111.1", "배포 확인", workdir=str(tmp_path), run_id="r1")
        발행 = 가짜발행()
        c = 체커(
            큐=큐, run_check=lambda job, outcome: 응답(ok=True, body="배포됐습니다"),
            발행=발행, 판정기=WatchResultReader(),
        )
        큐.시각["값"] = 2000.0
        c.check_once()

        assert 발행.게시내역 == [("C1", "111.1", "배포됐습니다", True)]

    def test_표식이_없으면_엔진을_안_부른다(self, 큐, tmp_path) -> None:
        from slack_cli_agent.reliability.watchresult import WatchResultReader

        결과 = tmp_path / ".watch-out"
        결과.mkdir()
        (결과 / "r1.out").write_text("진행 중\n", encoding="utf-8")
        큐.enqueue("C1", "111.1", "배포 확인", workdir=str(tmp_path), run_id="r1")
        호출: list[Any] = []
        c = 체커(
            큐=큐,
            run_check=기록하고_응답(호출, 응답(ok=True, body="x")),
            판정기=WatchResultReader(),
        )
        큐.시각["값"] = 2000.0
        c.check_once()

        assert 호출 == []


class Test포기한_건의_표식:
    """포기 경로는 소유자에게만 알린다. 원 메시지에 감시 표식이 남으면 채널
    쪽에서는 아직 지켜보는 중으로 보인다. 큐에는 없다 (sca-3p6).

    원본 bot.py 는 포기 경로에서 이모지를 안 건드린다. 여기서 다르게 하는
    이유가 이것이다.
    """

    def test_실패_표식_하나로_정리한다(self, 큐) -> None:
        """실제 ReactionMarker.remove 는 슬랙 예외를 삼킨다. 감시 표식을 따로
        떼면 그 호출이 조용히 실패한 뒤에도 실패 표식이 달려 mag 와 x 가 함께
        남는다. 떼는 일은 _settle 한 곳에만 둔다."""
        큐.enqueue("C1", "111.1", "배포 확인", msg_ts="222.2")
        리액션 = 가짜리액션()
        c = 체커(
            큐=큐, run_check=lambda job, outcome: 응답(ok=True, body="무시"),
            리액션=리액션, 설정값=설정(watch_job_max_age_sec=10.0),
            notify_owner=lambda 본문: None,
        )
        큐.시각["값"] = 99999.0
        c.check_once()

        assert 리액션.제거내역 == []
        assert 리액션.실패내역 == [("C1", "222.2")]
        assert 리액션.완료내역 == []

    def test_표식_대상_메시지가_없으면_아무것도_안_한다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인")
        리액션 = 가짜리액션()
        c = 체커(
            큐=큐, run_check=lambda job, outcome: 응답(ok=True, body="무시"),
            리액션=리액션, 설정값=설정(watch_job_max_age_sec=10.0),
            notify_owner=lambda 본문: None,
        )
        큐.시각["값"] = 99999.0
        c.check_once()

        assert 리액션.제거내역 == []
        assert 리액션.실패내역 == []

    def test_리액션이_터져도_한_번만_알린다(self, 큐) -> None:
        """표식 정리보다 큐 완료가 먼저다. 순서가 뒤집히면 예외가 완료를
        건너뛰어 다음 회차가 같은 건을 다시 포기 보고한다."""
        큐.enqueue("C1", "111.1", "배포 확인", msg_ts="222.2")

        class 터지는리액션(가짜리액션):
            def mark_failed(self, channel: str, ts: str) -> None:
                raise RuntimeError("리액션 실패")

        통지: list[str] = []
        c = 체커(
            큐=큐, run_check=lambda job, outcome: 응답(ok=True, body="무시"),
            리액션=터지는리액션(), 설정값=설정(watch_job_max_age_sec=10.0),
            notify_owner=통지.append,
        )
        큐.시각["값"] = 99999.0
        c.check_once()
        c.check_once()

        assert len(통지) == 1
        assert 큐.due(now=99999.0, min_gap=0.0) == []


class Test완료_경로의_리액션_장애:
    """완료 보고를 올린 뒤 표식 정리가 터져도 큐는 닫혀야 한다. 안 닫으면
    다음 회차가 같은 보고를 스레드에 다시 올린다 (sca-3p6 코덱스 검토)."""

    def test_완료_표식이_터져도_보고가_한_번만_나간다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인", msg_ts="222.2")

        class 터지는리액션(가짜리액션):
            def mark_done(self, channel: str, ts: str) -> None:
                raise RuntimeError("리액션 실패")

        발행 = 가짜발행()
        c = 체커(
            큐=큐, run_check=lambda job, outcome: 응답(ok=True, body=f"끝{WATCH_DONE_TAG}"),
            리액션=터지는리액션(), 발행=발행,
        )
        큐.시각["값"] = 2000.0
        c.check_once()
        c.check_once()

        assert len(발행.게시내역) == 1
        assert 큐.due(now=99999.0, min_gap=0.0) == []


class Test큐를_먼저_닫는다:
    """표식 정리가 예외를 삼키므로 순서가 뒤바뀌어도 지금은 결과가 같다. 그
    방어가 하나 깨졌을 때를 대비해 순서 자체를 고정한다 — `except Exception`
    은 BaseException 을 안 잡고, 표식 정리에 호출이 하나 더 붙을 수도 있다.
    """

    def _표식시점의_큐상태(self, 큐, *, 포기: bool) -> list:
        기록: list = []

        class 엿보는리액션(가짜리액션):
            def mark_done(그, channel: str, ts: str) -> None:
                기록.append(큐.due(now=99999.0, min_gap=0.0))
                super().mark_done(channel, ts)

            def mark_failed(그, channel: str, ts: str) -> None:
                기록.append(큐.due(now=99999.0, min_gap=0.0))
                super().mark_failed(channel, ts)

        c = 체커(
            큐=큐,
            run_check=lambda job, outcome: 응답(ok=True, body=f"끝{WATCH_DONE_TAG}"),
            리액션=엿보는리액션(),
            설정값=설정(watch_job_max_age_sec=10.0) if 포기 else None,
            notify_owner=(lambda 본문: None) if 포기 else None,
        )
        큐.시각["값"] = 99999.0 if 포기 else 2000.0
        c.check_once()
        return 기록

    def test_완료_경로는_표식_전에_큐가_닫혀_있다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인", msg_ts="222.2")
        assert self._표식시점의_큐상태(큐, 포기=False) == [[]]

    def test_포기_경로는_표식_전에_큐가_닫혀_있다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인", msg_ts="222.2")
        assert self._표식시점의_큐상태(큐, 포기=True) == [[]]


class 가짜감사:
    def __init__(self) -> None:
        self.기록: list[tuple[str, dict[str, Any]]] = []

    def record(self, kind: str, *, channel: str = "", thread_ts: str = "", **fields: Any) -> None:
        self.기록.append((kind, {"channel": channel, "thread_ts": thread_ts, **fields}))


class 터지는감사:
    def record(self, kind: str, *, channel: str = "", thread_ts: str = "", **fields: Any) -> None:
        raise RuntimeError("기록 실패")


class Test감시실행기록:
    """checks 와 last_run 은 현재 상태일 뿐이라 '감시가 돌아 본 기록이 없다' 가
    성립했다(sca-7cj, sca-j3d). 조건과 응답 본문은 슬랙 대화나 파일 내용을
    담을 수 있어 넣지 않는다.
    """

    def test_확인_한_회차가_남는다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인", msg_ts="222.2")
        감사 = 가짜감사()
        c = 체커(
            큐=큐,
            run_check=lambda job, outcome: 응답(ok=True, body=f"아직입니다 {WATCH_STILL_TAG}"),
            감사=감사,
        )
        큐.시각["값"] = 2000.0
        c.check_once()

        종류 = [kind for kind, _ in 감사.기록]
        assert WATCH_CHECKED_KIND in 종류
        기록 = dict(감사.기록)[WATCH_CHECKED_KIND]
        assert 기록["watch_job_id"] == 1
        assert 기록["ok"] is True
        assert 기록["channel"] == "C1"

    def test_기록에_조건과_본문을_안_담는다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "비밀조건입니다", msg_ts="222.2")
        감사 = 가짜감사()
        c = 체커(
            큐=큐,
            run_check=lambda job, outcome: 응답(ok=True, body=f"비밀본문입니다 {WATCH_STILL_TAG}"),
            감사=감사,
        )
        큐.시각["값"] = 2000.0
        c.check_once()

        적힌것 = str(감사.기록)
        assert "비밀조건입니다" not in 적힌것
        assert "비밀본문입니다" not in 적힌것

    def test_완료가_별도_종류로_남는다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인", msg_ts="222.2")
        감사 = 가짜감사()
        c = 체커(
            큐=큐,
            run_check=lambda job, outcome: 응답(ok=True, body=f"배포됐습니다 {WATCH_DONE_TAG}"),
            감사=감사,
        )
        큐.시각["값"] = 2000.0
        c.check_once()

        assert WATCH_FINISHED_KIND in [kind for kind, _ in 감사.기록]

    def test_포기가_별도_종류로_남는다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인", msg_ts="222.2")
        감사 = 가짜감사()
        c = 체커(
            큐=큐, run_check=lambda job, outcome: 응답(ok=True, body="안 불린다"),
            설정값=설정(watch_job_max_age_sec=10.0), 감사=감사,
        )
        큐.시각["값"] = 9999.0
        c.check_once()

        기록 = dict(감사.기록)
        assert WATCH_ABANDONED_KIND in 기록
        assert 기록[WATCH_ABANDONED_KIND]["watch_job_id"] == 1

    def test_엔진_예외는_종류만_남기고_메시지를_안_남긴다(self, 큐) -> None:
        def 터진다(job, outcome):
            raise RuntimeError("비밀예외메시지")

        큐.enqueue("C1", "111.1", "배포 확인", msg_ts="222.2")
        감사 = 가짜감사()
        c = 체커(큐=큐, run_check=터진다, 감사=감사)
        큐.시각["값"] = 2000.0
        c.check_once()

        기록 = dict(감사.기록)[WATCH_CHECKED_KIND]
        assert 기록["exception_type"] == "RuntimeError"
        assert "비밀예외메시지" not in str(감사.기록)
        assert 기록["ok"] is False

    def test_기록이_실패해도_감시는_계속_돈다(self, 큐) -> None:
        """이미 끝난 감시를 기록 실패로 잃으면 안 된다."""
        큐.enqueue("C1", "111.1", "배포 확인", msg_ts="222.2")
        발행 = 가짜발행()
        c = 체커(
            큐=큐,
            run_check=lambda job, outcome: 응답(ok=True, body=f"배포됐습니다 {WATCH_DONE_TAG}"),
            발행=발행, 감사=터지는감사(),
        )
        큐.시각["값"] = 2000.0
        c.check_once()

        assert 발행.게시내역
        assert 큐.due(3000.0, 0.0) == []

    def test_감사를_안_주면_그대로_돈다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인", msg_ts="222.2")
        발행 = 가짜발행()
        c = 체커(
            큐=큐,
            run_check=lambda job, outcome: 응답(ok=True, body=f"배포됐습니다 {WATCH_DONE_TAG}"),
            발행=발행,
        )
        큐.시각["값"] = 2000.0
        c.check_once()

        assert 발행.게시내역


class Test보고재발송:
    """발송 실패로 보고가 사라지는 것을 막는다(sca-dlv).

    작업 완료와 보고 발송 완료는 다른 사건이다. 둘을 한 표식으로 묶으면
    발송이 실패한 회차에 작업까지 닫혀 보고가 영영 안 나간다. 원 메시지에는
    완료 표식이 달리므로 사람은 끝난 줄 알고 결과를 기다린다.
    """

    def test_발송이_실패하면_보고를_남긴다(self, 큐: 시계달린큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인")
        c = 체커(
            큐=큐,
            run_check=lambda job, outcome: 응답(ok=True, body=f"배포 끝났다 {WATCH_DONE_TAG}"),
            발행=가짜발행(실패=True),
        )
        큐.시각["값"] = 2000.0
        c.check_once()

        남은보고 = 큐.pending_reports()
        assert [(r.channel, r.thread_ts) for r in 남은보고] == [("C1", "111.1")]
        assert "배포 끝났다" in 남은보고[0].body

    def test_다음_회차에_그_보고만_다시_보낸다(self, 큐: 시계달린큐) -> None:
        """엔진을 다시 부르지 않는다. 확인은 이미 끝났고 남은 것은 발송뿐이다."""
        큐.enqueue("C1", "111.1", "배포 확인")
        호출: list[Any] = []
        발행 = 가짜발행(실패=True)
        c = 체커(
            큐=큐,
            run_check=기록하고_응답(호출, 응답(ok=True, body=f"배포 끝났다 {WATCH_DONE_TAG}")),
            발행=발행,
        )
        큐.시각["값"] = 2000.0
        c.check_once()
        assert len(호출) == 1

        발행.실패 = False
        큐.시각["값"] = 3000.0
        c.check_once()

        assert len(호출) == 1
        assert [(게시[0], 게시[1]) for 게시 in 발행.게시내역] == [("C1", "111.1")]
        assert "배포 끝났다" in 발행.게시내역[0][2]

    def test_보내고_나면_다시_안_보낸다(self, 큐: 시계달린큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인")
        발행 = 가짜발행(실패=True)
        c = 체커(
            큐=큐,
            run_check=lambda job, outcome: 응답(ok=True, body=f"배포 끝났다 {WATCH_DONE_TAG}"),
            발행=발행,
        )
        큐.시각["값"] = 2000.0
        c.check_once()
        발행.실패 = False
        큐.시각["값"] = 3000.0
        c.check_once()
        큐.시각["값"] = 4000.0
        c.check_once()

        assert len(발행.게시내역) == 1
        assert 큐.pending_reports() == []

    def test_재발송도_실패하면_그대로_남는다(self, 큐: 시계달린큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인")
        c = 체커(
            큐=큐,
            run_check=lambda job, outcome: 응답(ok=True, body=f"배포 끝났다 {WATCH_DONE_TAG}"),
            발행=가짜발행(실패=True),
        )
        큐.시각["값"] = 2000.0
        c.check_once()
        큐.시각["값"] = 3000.0
        c.check_once()

        assert len(큐.pending_reports()) == 1

    def test_발송에_성공하면_남기지_않는다(self, 큐: 시계달린큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인")
        c = 체커(
            큐=큐,
            run_check=lambda job, outcome: 응답(ok=True, body=f"배포 끝났다 {WATCH_DONE_TAG}"),
        )
        큐.시각["값"] = 2000.0
        c.check_once()

        assert 큐.pending_reports() == []

    def test_채널의_rich_설정을_재발송에도_그대로_쓴다(self, 큐: 시계달린큐) -> None:
        """재발송이 평문으로 나가면 같은 보고가 회차마다 다른 모양이 된다."""
        큐.enqueue("C1", "111.1", "배포 확인")
        발행 = 가짜발행(실패=True)
        c = 체커(
            큐=큐,
            run_check=lambda job, outcome: 응답(ok=True, body=f"배포 끝났다 {WATCH_DONE_TAG}"),
            발행=발행, 채널목록=가짜채널목록({"C1": 가짜채널설정(rich=True)}),
        )
        큐.시각["값"] = 2000.0
        c.check_once()
        발행.실패 = False
        큐.시각["값"] = 3000.0
        c.check_once()

        assert 발행.게시내역[0][3] is True


class Test안_띄운_감시는_확인을_돌린다:
    """RUNNING 은 엔진 호출을 건너뛴다. 백그라운드 작업이 없는 조건 감시까지
    그렇게 보면 확인이 한 번도 안 돌아 checks 가 0 인 채 버려진다 (sca-vrs)."""

    def test_NOT_LAUNCHED면_엔진_확인을_돌린다(self, 큐) -> None:
        from slack_cli_agent.reliability.watchresult import WatchOutcome

        큐.enqueue("C1", "111.1", "커밋이 생겼는지 본다", workdir="/w", run_id="r1")
        호출: list[tuple[object, object]] = []

        def run_check(job, outcome):
            호출.append((job.id, outcome))
            return 응답(ok=True, body="아직입니다")

        c = 체커(큐=큐, run_check=run_check, 판정기=고정판정기(WatchOutcome.NOT_LAUNCHED))
        큐.시각["값"] = 2000.0
        c.check_once()

        assert [결과 for _작업, 결과 in 호출] == [WatchOutcome.NOT_LAUNCHED]
        assert [작업.checks for 작업 in 큐.due(now=99999.0, min_gap=0.0)] == [1]


class Test확인_횟수_상한을_건다:
    """due 와 expired 는 max_checks 를 받는데 check_once 가 안 넘겼다. 그래서
    조건 감시 하나가 24시간 동안 300초마다 엔진을 불렀다 - 최대 288회다
    (sca-2v2)."""

    def test_상한에_닿으면_더_안_묻는다(self, 큐) -> None:
        from slack_cli_agent.reliability.watchresult import WatchOutcome

        큐.enqueue("C1", "111.1", "커밋 확인", workdir="/w", run_id="r1")
        호출: list[object] = []

        def run_check(job, outcome):
            호출.append(job.id)
            return 응답(ok=True, body="아직")

        c = 체커(
            큐=큐,
            run_check=run_check,
            판정기=고정판정기(WatchOutcome.NOT_LAUNCHED),
            설정값=설정(watch_job_max_checks=2, watch_job_max_age_sec=99999.0),
            notify_owner=lambda _텍스트: None,
        )
        for 회차 in range(4):
            큐.시각["값"] = 2000.0 + 회차 * 1000
            c.check_once()

        assert len(호출) == 2

    def test_상한에_닿은_건은_포기_보고를_낸다(self, 큐) -> None:
        from slack_cli_agent.reliability.watchresult import WatchOutcome

        큐.enqueue("C1", "111.1", "커밋 확인", workdir="/w", run_id="r1")
        통지: list[str] = []
        c = 체커(
            큐=큐, run_check=lambda job, outcome: 응답(ok=True, body="아직"),
            판정기=고정판정기(WatchOutcome.NOT_LAUNCHED),
            설정값=설정(watch_job_max_checks=1, watch_job_max_age_sec=99999.0),
            notify_owner=통지.append,
        )
        for 회차 in range(3):
            큐.시각["값"] = 2000.0 + 회차 * 1000
            c.check_once()

        assert len(통지) == 1
        assert 큐.due(now=99999.0, min_gap=0.0) == []


class Test상한에_닿은_뒤_끝난_일도_보고된다:
    """상한을 due 로만 막으면 그 시점에 이미 끝나 있던 작업의 보고가 사라진다.
    expired 는 종료 상태를 보고 건너뛰고, due 는 횟수로 걸러 다시는 안 꺼낸다.
    그래서 작업이 done=0 인 채로 큐에 영영 남는다(코덱스 리뷰)."""

    def test_종료상태면_상한에_닿아도_완료_보고를_낸다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인", workdir="/w", run_id="r1")
        발행 = 가짜발행()
        통지: list[str] = []
        c = 체커(
            큐=큐,
            run_check=lambda job, outcome: 응답(ok=True, body="끝났습니다"),
            발행=발행,
            판정기=고정판정기(WatchOutcome.SUCCEEDED),
            설정값=설정(watch_job_max_checks=0, watch_job_max_age_sec=99999.0),
            notify_owner=통지.append,
        )
        큐.시각["값"] = 2000.0
        c.check_once()

        assert 발행.게시내역 == [("C1", "111.1", "끝났습니다", True)]
        assert 통지 == []
        assert 큐.expired(now=99999.0, max_age=0.0) == []

    def test_같은_회차에_확인이_두_번_돌지_않는다(self, 큐) -> None:
        """expired 에서 확인을 돌린 건을 due 가 다시 꺼내면 엔진을 한 회차에
        두 번 부른다. 그 건은 처리 완료로 표시해 아래 루프에서 뺀다."""
        큐.enqueue("C1", "111.1", "배포 확인", workdir="/w", run_id="r1")
        호출: list[int] = []

        def run_check(job, outcome):
            호출.append(job.id)
            return 응답(ok=False, body="", failure_reason="보고 생성 실패")

        c = 체커(
            큐=큐,
            run_check=run_check,
            판정기=고정판정기(WatchOutcome.SUCCEEDED),
            설정값=설정(watch_job_max_checks=0, watch_job_max_age_sec=99999.0),
        )
        큐.시각["값"] = 2000.0
        c.check_once()

        assert len(호출) == 1


class Test포기_사유를_가른다:
    """expired 가 '나이 초과 OR 횟수 초과' 를 한 목록으로 낸다. 사유를 안
    가르면 4시간 만에 횟수로 멈춘 건도 '하루 넘게' 로 보고된다(sca-uwq)."""

    def test_횟수_상한은_그렇게_보고한다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인", workdir="/w", run_id="r1")
        통지: list[str] = []
        c = 체커(
            큐=큐,
            run_check=lambda job, outcome: 응답(ok=True, body="아직"),
            판정기=고정판정기(WatchOutcome.NOT_LAUNCHED),
            설정값=설정(watch_job_max_checks=1, watch_job_max_age_sec=99999.0),
            notify_owner=통지.append,
        )
        for 회차 in range(3):
            큐.시각["값"] = 2000.0 + 회차 * 1000
            c.check_once()

        assert len(통지) == 1
        assert "하루" not in 통지[0]
        assert "확인 횟수 상한" in 통지[0]

    def test_시간_상한은_실제_경과로_보고한다(self, 큐) -> None:
        """상한이 설정값이라 '하루' 를 박으면 다른 설정에서 사실과 다르다."""
        큐.enqueue("C1", "111.1", "배포 확인")
        통지: list[str] = []
        c = 체커(
            큐=큐, run_check=lambda job, outcome: 응답(ok=True, body="안 불린다"),
            설정값=설정(watch_job_max_age_sec=10.0), notify_owner=통지.append,
        )
        큐.시각["값"] = 1000.0 + 2 * 3600
        c.check_once()

        assert len(통지) == 1
        assert "2시간 넘게 못 끝낸" in 통지[0]

    def test_기본_상한이면_문구가_원본과_같은_뜻이다(self, 큐) -> None:
        """원본 bot.py:6808 은 '하루 넘게' 다. 기본 24시간에서는 그 뜻이다."""
        큐.enqueue("C1", "111.1", "배포 확인")
        통지: list[str] = []
        c = 체커(
            큐=큐, run_check=lambda job, outcome: 응답(ok=True, body="안 불린다"),
            설정값=설정(watch_job_max_checks=99999), notify_owner=통지.append,
        )
        큐.시각["값"] = 1000.0 + 25 * 3600
        c.check_once()

        assert "25시간 넘게 못 끝낸" in 통지[0]

    def test_상한이_0이면_횟수가_사유다(self, 큐) -> None:
        """상한 0 은 확인을 한 번도 안 돌린다는 뜻이다. 그 건을 시간 초과로
        보고하면 실제로 걸린 조건과 다른 것을 알린다."""
        큐.enqueue("C1", "111.1", "배포 확인")
        통지: list[str] = []
        c = 체커(
            큐=큐, run_check=lambda job, outcome: 응답(ok=True, body="안 불린다"),
            설정값=설정(watch_job_max_checks=0, watch_job_max_age_sec=99999.0),
            notify_owner=통지.append,
        )
        큐.시각["값"] = 2000.0
        c.check_once()

        assert len(통지) == 1
        assert "확인 횟수 상한" in 통지[0]

    def test_둘_다_넘겼으면_횟수를_쓴다(self, 큐) -> None:
        """정책이다. 어느 쪽이 먼저 걸렸는지는 기록으로 못 가른다."""
        큐.enqueue("C1", "111.1", "배포 확인", workdir="/w", run_id="r1")
        통지: list[str] = []
        c = 체커(
            큐=큐,
            run_check=lambda job, outcome: 응답(ok=True, body="아직"),
            판정기=고정판정기(WatchOutcome.NOT_LAUNCHED),
            설정값=설정(watch_job_max_checks=1, watch_job_max_age_sec=1500.0),
            notify_owner=통지.append,
        )
        for 회차 in range(3):
            큐.시각["값"] = 2000.0 + 회차 * 1000
            c.check_once()

        assert "확인 횟수 상한" in 통지[0]

    def test_감사_기록에_사유가_남는다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인", workdir="/w", run_id="r1")
        감사 = 가짜감사()
        c = 체커(
            큐=큐,
            run_check=lambda job, outcome: 응답(ok=True, body="아직"),
            판정기=고정판정기(WatchOutcome.NOT_LAUNCHED),
            설정값=설정(watch_job_max_checks=1, watch_job_max_age_sec=99999.0),
            감사=감사,
        )
        for 회차 in range(3):
            큐.시각["값"] = 2000.0 + 회차 * 1000
            c.check_once()

        기록 = dict(감사.기록)
        assert 기록[WATCH_ABANDONED_KIND]["reason"] == "max_checks"

    def test_시간_초과_기록의_사유도_남는다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인")
        감사 = 가짜감사()
        c = 체커(
            큐=큐, run_check=lambda job, outcome: 응답(ok=True, body="안 불린다"),
            설정값=설정(watch_job_max_age_sec=10.0), 감사=감사,
        )
        큐.시각["값"] = 9999.0
        c.check_once()

        assert dict(감사.기록)[WATCH_ABANDONED_KIND]["reason"] == "max_age"

    @pytest.mark.parametrize(
        "경과, 기대",
        [(90000.0, "25시간"), (5400.0, "1시간"), (600.0, "10분"), (30.0, "30초")],
    )
    def test_경과는_단위를_바꿔_읽힌다(self, 경과: float, 기대: str) -> None:
        from slack_cli_agent.reliability.watchrunner import _elapsed_text

        assert _elapsed_text(경과) == 기대
