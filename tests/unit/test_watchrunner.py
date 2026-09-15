"""감시 큐 실행 계층 시험.

`WatchJobChecker` 가 큐에서 확인 대상을 꺼내 `run_check` 로 넘기고, 그 응답에
따라 완료·포기·재확인을 가르는지를 검증한다. 엔진 실행 자체는 흉내 내지
않는다 — `run_check` 콜백이 그 자리를 대신한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.engine.base import EngineResponse
from slack_cli_agent.guard.watch import WATCH_DONE_TAG, WATCH_MARK_EMOJI, WATCH_STILL_TAG
from slack_cli_agent.reliability.watchjobs import WatchJob, WatchJobQueue
from slack_cli_agent.reliability.watchrunner import WatchJobChecker


def 응답(*, ok: bool, body: str, failure_reason: str | None = None) -> EngineResponse:
    return EngineResponse(
        ok=ok, body=body, session_id=None, model_actual=None,
        elapsed=0.0, turns=None, usage=None, failure_reason=failure_reason,
    )


@pytest.fixture
def 큐(database):
    시각 = {"값": 1000.0}
    q = WatchJobQueue(database, now=lambda: 시각["값"])
    q.시각 = 시각
    return q


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

    def remove(self, channel: str, ts: str, name: str) -> None:
        self.제거내역.append((channel, ts, name))

    def mark_done(self, channel: str, ts: str) -> None:
        self.완료내역.append((channel, ts))


def 설정(**overrides: Any) -> RuntimeSettings:
    return RuntimeSettings().override(overrides)


def 체커(*, 큐, run_check, 발행=None, 채널목록=None, 설정값=None, 리액션=None, notify_owner=None):
    return WatchJobChecker(
        queue=큐,
        run_check=run_check,
        publisher=발행 or 가짜발행(),
        channels=채널목록 or 가짜채널목록(),
        settings=설정값 or 설정(),
        reactions=리액션,
        notify_owner=notify_owner,
        now=lambda: 큐.시각["값"],
    )


class Test완료처리:
    def test_완료태그가있으면게시하고큐에서완료로표시한다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인", msg_ts="222.2")
        발행 = 가짜발행()
        c = 체커(
            큐=큐, run_check=lambda job: 응답(ok=True, body=f"배포됐습니다 {WATCH_DONE_TAG}"),
            발행=발행,
        )
        큐.시각["값"] = 2000.0
        c.check_once()

        assert 발행.게시내역 == [("C1", "111.1", "배포됐습니다", False)]
        assert 큐.due(now=99999.0, min_gap=0.0) == []
        assert 큐.expired(now=99999.0, max_age=0.0) == []

    def test_게시한본문에완료태그문자열이남지않는다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인")
        발행 = 가짜발행()
        c = 체커(
            큐=큐,
            run_check=lambda job: 응답(ok=True, body=f"결과 문구\n{WATCH_DONE_TAG}"),
            발행=발행,
        )
        큐.시각["값"] = 2000.0
        c.check_once()

        assert WATCH_DONE_TAG not in 발행.게시내역[0][2]

    def test_완료시감시표식을떼고완료표식을단다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인", msg_ts="222.2")
        리액션 = 가짜리액션()
        c = 체커(
            큐=큐, run_check=lambda job: 응답(ok=True, body=f"끝{WATCH_DONE_TAG}"),
            리액션=리액션,
        )
        큐.시각["값"] = 2000.0
        c.check_once()

        assert 리액션.제거내역 == [("C1", "222.2", WATCH_MARK_EMOJI)]
        assert 리액션.완료내역 == [("C1", "222.2")]

    def test_msg_ts가비어있으면리액션을건드리지않는다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인")  # msg_ts 기본값 ""
        리액션 = 가짜리액션()
        c = 체커(
            큐=큐, run_check=lambda job: 응답(ok=True, body=f"끝{WATCH_DONE_TAG}"),
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
            큐=큐, run_check=lambda job: 응답(ok=True, body=f"끝{WATCH_DONE_TAG}"),
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
            큐=큐, run_check=lambda job: 응답(ok=True, body=f"끝{WATCH_DONE_TAG}"),
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
        c = 체커(큐=큐, run_check=lambda job: 응답(ok=True, body=WATCH_DONE_TAG), 발행=발행)
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
        c = 체커(큐=큐, run_check=lambda job: 응답(ok=True, body=f"  \n {WATCH_DONE_TAG}\n "), 발행=발행)
        큐.시각["값"] = 2000.0
        c.check_once()

        assert 발행.게시내역[0][2].strip()

    def test_본문이_있으면_그대로_올린다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "배포 확인")
        발행 = 가짜발행()
        c = 체커(큐=큐, run_check=lambda job: 응답(ok=True, body=f"끝났습니다 {WATCH_DONE_TAG}"), 발행=발행)
        큐.시각["값"] = 2000.0
        c.check_once()

        assert 발행.게시내역[0][2] == "끝났습니다"


class Test미완료처리:
    def test_아직안끝났으면게시하지않고확인횟수만갱신한다(self, 큐) -> None:
        작업_id = 큐.enqueue("C1", "111.1", "배포 확인")
        발행 = 가짜발행()
        c = 체커(
            큐=큐, run_check=lambda job: 응답(ok=True, body=f"아직 {WATCH_STILL_TAG}"),
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
        c = 체커(큐=큐, run_check=lambda job: 응답(ok=False, body=""))
        큐.시각["값"] = 2000.0
        c.check_once()  # 예외가 안 나면 통과

        assert 큐.due(now=99999.0, min_gap=0.0)[0].checks == 1

    def test_run_check가예외를내도다음건의확인이계속된다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "실패할 건")
        큐.enqueue("C1", "111.2", "정상 확인될 건")

        def run_check(job: WatchJob) -> EngineResponse:
            if job.condition == "실패할 건":
                raise RuntimeError("엔진 오류")
            return 응답(ok=True, body=f"아직 {WATCH_STILL_TAG}")

        c = 체커(큐=큐, run_check=run_check)
        큐.시각["값"] = 2000.0
        c.check_once()

        남은 = {작업.condition: 작업.checks for 작업 in 큐.due(now=99999.0, min_gap=0.0)}
        assert 남은 == {"실패할 건": 1, "정상 확인될 건": 1}


class Test확인실패진단:
    """확인이 실패했을 때 무엇이 로그에 남는가.

    실패가 엔진 오류인지 모델의 태그 누락인지 로그만으로 갈려야 한다. 그런데
    응답 본문과 감시 조건은 슬랙 대화·파일 내용이 그대로 들어올 수 있으므로
    로그에 남기지 않는다.
    """

    def test_엔진실패는_원인과_작업식별자를_남긴다(self, 큐, caplog) -> None:
        작업_id = 큐.enqueue("C1", "111.1", "배포 확인")
        c = 체커(큐=큐, run_check=lambda job: 응답(ok=False, body="", failure_reason="timeout"))
        큐.시각["값"] = 2000.0
        with caplog.at_level("WARNING"):
            c.check_once()

        기록 = "\n".join(r.getMessage() for r in caplog.records)
        assert "감시 확인 엔진 실패" in 기록
        assert "timeout" in 기록
        assert f"작업 {작업_id}" in 기록
        assert 큐.due(now=99999.0, min_gap=0.0)[0].checks == 1

    def test_태그누락은_엔진실패와_다른_문구로_남는다(self, 큐, caplog) -> None:
        큐.enqueue("C1", "111.1", "배포 확인")
        c = 체커(큐=큐, run_check=lambda job: 응답(ok=True, body="그냥 답했다"))
        큐.시각["값"] = 2000.0
        with caplog.at_level("WARNING"):
            c.check_once()

        기록 = "\n".join(r.getMessage() for r in caplog.records)
        assert "감시 확인 태그 누락" in 기록
        assert "감시 확인 엔진 실패" not in 기록

    def test_응답본문과_감시조건은_로그에_안_남는다(self, 큐, caplog) -> None:
        큐.enqueue("C1", "111.1", "xoxb-비밀토큰-조건")
        c = 체커(큐=큐, run_check=lambda job: 응답(ok=True, body="xoxb-비밀토큰-본문"))
        큐.시각["값"] = 2000.0
        with caplog.at_level("WARNING"):
            c.check_once()

        기록 = "\n".join(r.getMessage() for r in caplog.records)
        assert "비밀토큰" not in 기록

    def test_엔진실패_경로에서도_응답본문이_로그에_안_남는다(self, 큐, caplog) -> None:
        큐.enqueue("C1", "111.1", "배포 확인")
        c = 체커(
            큐=큐,
            run_check=lambda job: 응답(ok=False, body="xoxb-비밀토큰-본문", failure_reason="timeout"),
        )
        큐.시각["값"] = 2000.0
        with caplog.at_level("WARNING"):
            c.check_once()

        assert "비밀토큰" not in "\n".join(r.getMessage() for r in caplog.records)

    def test_정상완료와_진행중에는_경고가_안_남는다(self, 큐, caplog) -> None:
        큐.enqueue("C1", "111.1", "끝난 건")
        큐.enqueue("C1", "111.2", "진행중인 건")

        def run_check(job: WatchJob) -> EngineResponse:
            태그 = WATCH_DONE_TAG if job.condition == "끝난 건" else WATCH_STILL_TAG
            return 응답(ok=True, body=f"보고 {태그}")

        c = 체커(큐=큐, run_check=run_check, 발행=가짜발행())
        큐.시각["값"] = 2000.0
        with caplog.at_level("WARNING"):
            c.check_once()

        assert [r.getMessage() for r in caplog.records] == []

    def test_run_check_예외는_작업식별자와_추적을_남긴다(self, 큐, caplog) -> None:
        작업_id = 큐.enqueue("C1", "111.1", "터지는 건")

        def run_check(job: WatchJob) -> EngineResponse:
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
            run_check=lambda job: 응답(ok=True, body=f"아직 {WATCH_STILL_TAG}"),
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
            큐=큐, run_check=lambda job: 응답(ok=True, body=f"아직 {WATCH_STILL_TAG}"),
            설정값=설정(watch_job_max_age_sec=500.0),
        )
        큐.시각["값"] = 2000.0
        c.check_once()  # notify_owner 없이도 예외 없이 끝나야 한다

        assert 큐.due(now=99999.0, min_gap=0.0) == []
        assert 큐.expired(now=99999.0, max_age=0.0) == []

    def test_같은회차에서포기처리한건에는엔진을부르지않는다(self, 큐) -> None:
        큐.enqueue("C1", "111.1", "오래된 건")
        불린횟수 = {"값": 0}

        def run_check(job: WatchJob) -> EngineResponse:
            불린횟수["값"] += 1
            return 응답(ok=True, body=f"아직 {WATCH_STILL_TAG}")

        c = 체커(
            큐=큐, run_check=run_check,
            설정값=설정(watch_job_max_age_sec=500.0, watch_job_min_gap_sec=0.0),
        )
        큐.시각["값"] = 2000.0
        c.check_once()

        assert 불린횟수["값"] == 0
