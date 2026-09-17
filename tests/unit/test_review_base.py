"""review/base.py 의 ReviewTask 공통 흐름과 표 조립 도우미 테스트.

원본 bot.py 의 run_postmortem/_run_postmortem 계열 셋이
공유하던 흐름(대상 조회 -> 처리중 표시 -> 대화록/실행기록 조회 -> 모델 호출
-> 실패 처리 -> 구분선 분할 -> 채널 게시 -> 원장 기록)을 ReviewTask(ABC) 로
하나로 합쳤다. 여기서는 최소한의 가짜 하위 클래스로 그 공통 흐름만 검증한다.

as_table/cell 의 기대값은 원본 as_table()/cell() 을 AST 추출해 실제로
실행해서 얻었다.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from review_support import header_body_lines, recorded

from slack_cli_agent.engine.base import EngineResponse
from slack_cli_agent.review.base import (
    ReviewTarget,
    ReviewTask,
    as_table,
    cell,
    model_effort_cell,
    run_info_rows,
)
from slack_cli_agent.review.ledger import ReviewLedger


class FakeMessageLookup:
    def __init__(self, msg: Mapping[str, Any] | None) -> None:
        self._msg = msg

    def find(self, channel: str, ts: str) -> Mapping[str, Any] | None:
        return self._msg


class FakeTranscript:
    def transcript(self, channel: str, thread_ts: str) -> str:
        return f"[대화록:{channel}:{thread_ts}]"


class FakeAnswerFinder:
    def __init__(self, record: Mapping[str, Any] | None = None) -> None:
        self._record = record

    def find(self, channel: str, thread_ts: str, text: str) -> Mapping[str, Any] | None:
        return self._record


class FakeReactions:
    def __init__(self) -> None:
        self.processing: list[tuple[str, str]] = []
        self.cleared: list[tuple[str, str]] = []

    def mark_processing(self, channel: str, ts: str) -> None:
        self.processing.append((channel, ts))

    def clear_processing(self, channel: str, ts: str) -> None:
        self.cleared.append((channel, ts))


class FakePermalinks:
    def permalink(self, channel: str, ts: str) -> str:
        return f"https://slack.example/{channel}/{ts}"


class FakePublisher:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str | None, str, bool]] = []
        self._next_ts = 1

    def post(self, channel: str, thread_ts: str | None, text: str, *, rich: bool) -> str | None:
        self.posts.append((channel, thread_ts, text, rich))
        ts = f"parent.{self._next_ts}"
        self._next_ts += 1
        return ts


class FakeEngine:
    def __init__(self, responses: list[EngineResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str | None, bool]] = []
        self.progress_logs: list[Path | None] = []

    def run(
        self, prompt: str, session_id: str | None, resume: bool, progress_log: Path | None = None
    ) -> EngineResponse:
        self.calls.append((prompt, session_id, resume))
        self.progress_logs.append(progress_log)
        return self._responses.pop(0)


class FakeAudit:
    def __init__(self, fail: bool = False) -> None:
        self.rows: list[tuple[str, dict[str, Any]]] = []
        self._fail = fail

    def record(self, kind: str, *, channel: str = "", thread_ts: str = "", **fields: Any) -> None:
        if self._fail:
            raise RuntimeError("기록 실패")
        self.rows.append((kind, {"channel": channel, "thread_ts": thread_ts, **fields}))


class FakeReviewProgress:
    """진행 표시 port 대역. 실제 것은 슬랙에 글을 올렸다 지운다."""

    def __init__(self, log_path: Path | None = Path("/tmp/진행.log"), fail: bool = False) -> None:
        self._log_path = log_path
        self._fail = fail
        self.opened: list[tuple[str, str]] = []
        self.closed = 0

    @contextmanager
    def display(self, target: ReviewTarget, thread_ts: str) -> Iterator[Path | None]:
        if self._fail:
            raise RuntimeError("진행 표시를 열지 못했다")
        self.opened.append((target.channel, thread_ts))
        try:
            yield self._log_path
        finally:
            self.closed += 1


def _ok_response(body: str) -> EngineResponse:
    return EngineResponse(
        ok=True, body=body, session_id="s1", model_actual=None,
        elapsed=1.0, turns=1, usage=None,
    )


def _fail_response(body: str = "실패") -> EngineResponse:
    return EngineResponse(
        ok=False, body=body, session_id=None, model_actual=None,
        elapsed=1.0, turns=None, usage=None,
    )


class FakeReviewTask(ReviewTask):
    """테스트 전용 최소 구현. 실제 부검/디버그/서식 점검은 별도 파일에서 검증한다."""

    log_name = "fake_kind"
    emoji = "test"

    def build_prompt(self, target: ReviewTarget, *, transcript: str, flagged: str, question: str) -> str:
        return f"prompt:{flagged}"

    def header_title(self, target: ReviewTarget) -> str:
        return "head"

    def header_rows(
        self, target: ReviewTarget, record: Mapping[str, Any] | None, link: str
    ) -> list[tuple[str, str]]:
        return []


@dataclass
class Rig:
    ledger: ReviewLedger
    reactions: FakeReactions
    publisher: FakePublisher
    engine: FakeEngine
    message_lookup: FakeMessageLookup
    task: ReviewTask
    audit: FakeAudit


_MISSING = object()


def make_rig(database, *, msg=_MISSING, engine_responses=None, record=None, progress=None,
             audit=None, owner_only_channels=frozenset({"TS"})) -> Rig:
    ledger = ReviewLedger(database)
    reactions = FakeReactions()
    publisher = FakePublisher()
    engine = FakeEngine(engine_responses or [_ok_response("요약===상세===상세내용")])
    message_lookup = FakeMessageLookup(
        {"thread_ts": "100.0", "text": "지목한 답변"} if msg is _MISSING else msg
    )
    audit = audit or FakeAudit()
    task = FakeReviewTask(
        ledger=ledger,
        message_lookup=message_lookup,
        transcript=FakeTranscript(),
        answer_finder=FakeAnswerFinder(record),
        reactions=reactions,
        permalinks=FakePermalinks(),
        publisher=publisher,
        engine=engine,
        troubleshoot_channel="TS",
        owner_only_channels=owner_only_channels,
        progress=progress,
        audit=audit,
    )
    return Rig(ledger, reactions, publisher, engine, message_lookup, task, audit)


class Test중복실행방지:
    def test_이미완료된대상은다시실행하지않는다(self, database) -> None:
        rig = make_rig(database)
        rig.ledger.complete("fake_kind", "C1", "1.1", by="U1")
        rig.task.run(ReviewTarget(channel="C1", ts="1.1", by_user="U2", channel_name="채널", rich=True))
        assert rig.engine.calls == []


class Test정상흐름:
    def test_요약과상세를나눠올린다(self, database) -> None:
        rig = make_rig(database)
        rig.task.run(ReviewTarget(channel="C1", ts="1.1", by_user="U2", channel_name="채널", rich=True))
        assert len(rig.publisher.posts) == 2
        parent_channel, parent_thread, parent_text, parent_rich = rig.publisher.posts[0]
        assert parent_channel == "TS"
        assert parent_thread is None
        assert "head" in parent_text and "요약" in parent_text
        assert parent_rich is True
        _detail_channel, detail_thread, detail_text, _ = rig.publisher.posts[1]
        assert detail_thread == "parent.1"
        assert detail_text == "상세내용"

    def test_완료기록을남긴다(self, database) -> None:
        rig = make_rig(database)
        rig.task.run(ReviewTarget(channel="C1", ts="1.1", by_user="U2", channel_name="채널", rich=True))
        rec = recorded(rig.ledger, "fake_kind", "C1", "1.1")
        assert rec.status == "완료"
        assert rec.by == "U2"

    def test_처리중표시를달았다지운다(self, database) -> None:
        rig = make_rig(database)
        rig.task.run(ReviewTarget(channel="C1", ts="1.1", by_user="U2", channel_name="채널", rich=True))
        assert rig.reactions.processing == [("C1", "1.1")]
        assert rig.reactions.cleared == [("C1", "1.1")]

    def test_구분선이없으면전체를요약으로올린다(self, database) -> None:
        rig = make_rig(database, engine_responses=[_ok_response("구분선없는본문")])
        rig.task.run(ReviewTarget(channel="C1", ts="1.1", by_user="U2", channel_name="채널", rich=True))
        assert len(rig.publisher.posts) == 1
        assert "구분선없는본문" in rig.publisher.posts[0][2]


class Test대상이없을때:
    def test_메시지를못찾으면조용히포기한다(self, database) -> None:
        rig = make_rig(database, msg=None)
        rig.task.run(ReviewTarget(channel="C1", ts="1.1", by_user="U2", channel_name="채널", rich=True))
        assert rig.engine.calls == []
        assert rig.ledger.find("fake_kind", "C1", "1.1") is None


class Test모델호출실패:
    def test_실패하면기록을지우고안내를올린다(self, database) -> None:
        rig = make_rig(database, engine_responses=[_fail_response("엔진오류")])
        rig.task.run(ReviewTarget(channel="C1", ts="1.1", by_user="U2", channel_name="채널", rich=True))
        assert rig.ledger.find("fake_kind", "C1", "1.1") is None
        assert len(rig.publisher.posts) == 1
        assert "엔진오류" in rig.publisher.posts[0][2]


class Test예외발생:
    def test_중간에예외가나면기록을지운다(self, database) -> None:
        class BrokenMessageLookup:
            def find(self, channel: str, ts: str) -> Mapping[str, Any] | None:
                raise RuntimeError("조회 중단")

        rig = make_rig(database)
        rig.task._message_lookup = BrokenMessageLookup()
        rig.task.run(ReviewTarget(channel="C1", ts="1.1", by_user="U2", channel_name="채널", rich=True))
        assert rig.ledger.find("fake_kind", "C1", "1.1") is None
        assert len(rig.publisher.posts) == 1
        assert "중단" in rig.publisher.posts[0][2]


class Test표조립도우미:
    def test_cell은파이프와줄바꿈을치환한다(self) -> None:
        assert cell("a|b\nc") == "a/b c"

    def test_cell은빈값을대시로바꾼다(self) -> None:
        assert cell("") == "-"

    def test_as_table은빈목록이면빈문자열이다(self) -> None:
        assert as_table([]) == ""

    def test_as_table은파이프표를만든다(self) -> None:
        rows = [("a", "b|c\nd"), ("e", "")]
        assert as_table(rows) == "| 항목 | 값 |\n|---|---|\n| a | b/c d |\n| e | - |"

    def test_model_effort_cell은실제모델이다르면함께보인다(self) -> None:
        rec = {"model": "opus", "model_actual": "sonnet", "effort": "high"}
        assert model_effort_cell(rec) == "opus (실제 sonnet) / high"

    def test_model_effort_cell은값이없으면물음표다(self) -> None:
        assert model_effort_cell(None) == "? / ?"

    def test_run_info_rows는기록이없으면못찾았다고적는다(self) -> None:
        assert run_info_rows(None) == [("실행 정보", "감사 기록에서 이 답변을 찾지 못해 뺐습니다")]

    def test_run_info_rows는턴수가있으면소요에함께적는다(self) -> None:
        rows = run_info_rows({"model": "opus", "effort": "high", "elapsed": 12.3, "num_turns": 4})
        assert rows == [("모델 / effort", "opus / high"), ("소요", "12.3초, 4턴")]


def make_header_task(cls: type[ReviewTask]) -> ReviewTask:
    """머리말만 보는 테스트용. 협력 객체는 build_header 가 쓰지 않아 None 으로 둔다."""
    없음 = cast(Any, None)
    return cls(
        ledger=없음, message_lookup=없음, transcript=없음, answer_finder=없음,
        reactions=없음, permalinks=없음, publisher=없음, engine=없음,
        troubleshoot_channel="TS",
    )


class Test머리말조립:
    """요청자 줄이 표 밖에 덧붙어 같은 머리말에서 같은 종류의 값이
    표와 평문으로 갈렸다. 세 점검 모두 표 한 벌로만 낸다."""

    def test_요청자행이표안에들어간다(self) -> None:
        class 행있는점검(FakeReviewTask):
            def header_rows(self, target, record, link):
                return [("대화", target.channel_name)]

        target = ReviewTarget(channel="C1", ts="1.1", by_user="U1", channel_name="회의방", rich=True)
        header = make_header_task(행있는점검).build_header(target, None, "")
        assert header_body_lines(header) == [
            "| 항목 | 값 |",
            "|---|---|",
            "| 대화 | 회의방 |",
            "| 요청한 사람 | <@U1> |",
        ]

    def test_라벨은점검종류마다다르게둘수있다(self) -> None:
        class 지적받는점검(FakeReviewTask):
            requester_label = "지적한 사람"

        target = ReviewTarget(channel="C1", ts="1.1", by_user="U1", channel_name="회의방", rich=True)
        header = make_header_task(지적받는점검).build_header(target, None, "")
        assert "| 지적한 사람 | <@U1> |" in header


class Test점검이_도는_동안_진행_신호를_낸다:
    """경단을 붙이면 눈 이모지만 붙고 결과까지 아무 신호가 없었다(sca-tfd).

    실측으로 신지는 4분, 레이는 900초 제한을 넘겼다. 그 사이 사용자에게는
    작동하지 않는 것과 구분되지 않는다.
    """

    def test_엔진을_부르는_동안_진행_표시를_연다(self, database) -> None:
        진행 = FakeReviewProgress()
        rig = make_rig(database, progress=진행)
        rig.task.run(ReviewTarget(channel="C1", ts="1.1", by_user="U2", channel_name="채널", rich=True))
        assert 진행.opened == [("C1", "100.0")]
        assert 진행.closed == 1

    def test_진행_표시가_정한_로그_경로를_엔진에_넘긴다(self, database) -> None:
        """엔진의 도구 훅이 그 파일에 쓰고, 진행 표시가 그 파일을 읽는다.
        두 경로가 다르면 표시가 '작업 중' 에서 멈춘다."""
        진행 = FakeReviewProgress(log_path=Path("/tmp/특정.log"))
        rig = make_rig(database, progress=진행)
        rig.task.run(ReviewTarget(channel="C1", ts="1.1", by_user="U2", channel_name="채널", rich=True))
        assert rig.engine.progress_logs == [Path("/tmp/특정.log")]

    def test_진행_표시가_없으면_로그_경로_없이_그대로_돈다(self, database) -> None:
        rig = make_rig(database)
        rig.task.run(ReviewTarget(channel="C1", ts="1.1", by_user="U2", channel_name="채널", rich=True))
        assert rig.engine.progress_logs == [None]
        assert recorded(rig.ledger, "fake_kind", "C1", "1.1").status == "완료"

    def test_진행_표시가_실패해도_점검은_끝낸다(self, database) -> None:
        """표시는 꾸밈이다. 그것 때문에 점검이 중단되면 안 된다."""
        rig = make_rig(database, progress=FakeReviewProgress(fail=True))
        rig.task.run(ReviewTarget(channel="C1", ts="1.1", by_user="U2", channel_name="채널", rich=True))
        assert recorded(rig.ledger, "fake_kind", "C1", "1.1").status == "완료"


class 재시도하는Task(FakeReviewTask):
    def retry_on_missing_split(self) -> bool:
        return True

    def missing_split_prompt(self) -> str:
        return "구분선을 넣어 다시 써라"


def _재시도_rig(database, 응답들) -> Rig:
    rig = make_rig(database, engine_responses=응답들)
    rig.task.__class__ = 재시도하는Task
    return rig


class Test세션ID는엔진이정한다:
    """세션 ID 형식은 그 요청을 실제로 받는 CLI 의 것이다(sca-k6s).

    지금은 클로드·코덱스·제미나이가 모두 하이픈 포함 UUID 를 받아 우연히
    맞는다. 형식이 다른 엔진이 들어오면 CLI 종료코드로만 실패가 나서 로그로는
    원인이 안 보인다.
    """

    def test_점검은_세션_ID_를_직접_만들지_않는다(self, database) -> None:
        rig = make_rig(database)
        rig.task.run(ReviewTarget(channel="C1", ts="1.1", by_user="U2", channel_name="채널", rich=True))
        assert rig.engine.calls[0][1] is None

    def test_재시도는_1차가_실제로_쓴_세션을_이어간다(self, database) -> None:
        """엔진이 정한 ID 를 안 받아 오면 재시도가 다른 대화로 간다."""
        rig = _재시도_rig(
            database,
            [
                EngineResponse(
                    ok=True, body="구분선없는본문", session_id="엔진이-정한-id",
                    model_actual=None, elapsed=1.0, turns=1, usage=None,
                ),
                _ok_response("요약===상세===상세내용"),
            ],
        )
        rig.task.run(ReviewTarget(channel="C1", ts="1.1", by_user="U2", channel_name="채널", rich=True))
        _, 재시도_세션, 이어가기 = rig.engine.calls[1]
        assert (재시도_세션, 이어가기) == ("엔진이-정한-id", True)

    def test_1차가_세션_ID_를_안_주면_재시도하지_않는다(self, database) -> None:
        """이어갈 대상이 없는데 이어가기로 부르면 다른 대화에 붙거나 실패한다."""
        rig = _재시도_rig(
            database,
            [
                EngineResponse(
                    ok=True, body="구분선없는본문", session_id=None,
                    model_actual=None, elapsed=1.0, turns=1, usage=None,
                ),
            ],
        )
        rig.task.run(ReviewTarget(channel="C1", ts="1.1", by_user="U2", channel_name="채널", rich=True))
        assert len(rig.engine.calls) == 1
        assert "구분선없는본문" in rig.publisher.posts[0][2]


class Test점검_실행_기록:
    """sca-fy5 — 점검은 엔진을 몇 분씩 쓰는데 audit 에 한 줄도 안 남았다.
    소요 분포가 없으면 제한시간을 어떻게 잡을지 정할 근거가 없다."""

    def test_엔진_호출마다_소요와_결과를_남긴다(self, database) -> None:
        rig = make_rig(database)
        rig.task.run(ReviewTarget(channel="C1", ts="1.1", by_user="U2", channel_name="채널", rich=True))
        [(kind, fields)] = rig.audit.rows
        assert kind == "review"
        assert fields["review_kind"] == "fake_kind"
        assert fields["channel"] == "C1"
        assert fields["target_ts"] == "1.1"
        assert fields["elapsed"] == 1.0
        assert fields["ok"] is True
        assert fields["attempt"] == "main"

    def test_실패한_호출도_사유와_함께_남긴다(self, database) -> None:
        response = EngineResponse(
            ok=False, body="시간 초과", session_id=None, model_actual=None,
            elapsed=900.0, turns=None, usage=None, failure_reason="timeout", engine="gemini",
        )
        rig = make_rig(database, engine_responses=[response])
        rig.task.run(ReviewTarget(channel="C1", ts="1.1", by_user="U2", channel_name="채널", rich=True))
        [(_, fields)] = rig.audit.rows
        assert fields["ok"] is False
        assert fields["failure"] == "timeout"
        assert fields["elapsed"] == 900.0
        assert fields["engine"] == "gemini"

    def test_기록이_실패해도_점검은_끝난다(self, database) -> None:
        rig = make_rig(database, audit=FakeAudit(fail=True))
        rig.task.run(ReviewTarget(channel="C1", ts="1.1", by_user="U2", channel_name="채널", rich=True))
        assert recorded(rig.ledger, "fake_kind", "C1", "1.1").status == "완료"

    def test_기록기가_없어도_돈다(self, database) -> None:
        rig = make_rig(database)
        rig.task._audit = None
        rig.task.run(ReviewTarget(channel="C1", ts="1.1", by_user="U2", channel_name="채널", rich=True))
        assert recorded(rig.ledger, "fake_kind", "C1", "1.1").status == "완료"


class Test점검보고도소유자전용채널에만낸다:
    """점검 보고는 원 채널 전문을 바탕으로 만든 모델 출력이라 같은 채널
    경계를 넘는다. 느린 요청 보고에만 걸려 있던 강제를 여기도 건다 (sca-psr).
    """

    def test_소유자전용이아니면점검을돌리지않는다(self, database) -> None:
        rig = make_rig(database, owner_only_channels=frozenset())
        rig.task.run(ReviewTarget(channel="C1", ts="1.1", by_user="U2", channel_name="채널", rich=True))
        assert rig.engine.calls == []
        assert rig.publisher.posts == []

    def test_돌리지않은것을원장에도남기지않는다(self, database) -> None:
        """진행 행이 남으면 설정을 고친 뒤에도 재시도가 막힌다."""
        rig = make_rig(database, owner_only_channels=frozenset())
        rig.task.run(ReviewTarget(channel="C1", ts="1.1", by_user="U2", channel_name="채널", rich=True))
        assert rig.ledger.find("fake_kind", "C1", "1.1") is None

    def test_소유자전용이면그대로돈다(self, database) -> None:
        rig = make_rig(database, owner_only_channels=frozenset({"TS"}))
        rig.task.run(ReviewTarget(channel="C1", ts="1.1", by_user="U2", channel_name="채널", rich=True))
        assert len(rig.engine.calls) == 1

    def test_끌때사유를로그에남긴다(self, database, caplog) -> None:
        """안 도는 것과 리액션이 안 온 것이 같은 모습이면 원인을 못 가린다."""
        with caplog.at_level(logging.WARNING):
            make_rig(database, owner_only_channels=frozenset())
        assert any("owner_only_channels" in r.getMessage() for r in caplog.records)
