"""review/base.py 의 ReviewTask 공통 흐름과 표 조립 도우미 테스트.

원본 bot.py 의 run_postmortem/_run_postmortem 계열 셋이
공유하던 흐름(대상 조회 -> 처리중 표시 -> 대화록/실행기록 조회 -> 모델 호출
-> 실패 처리 -> 구분선 분할 -> 채널 게시 -> 원장 기록)을 ReviewTask(ABC) 로
하나로 합쳤다. 여기서는 최소한의 가짜 하위 클래스로 그 공통 흐름만 검증한다.

as_table/cell 의 기대값은 원본 as_table()/cell() 을 AST 추출해 실제로
실행해서 얻었다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from slack_cli_agent.engine.base import EngineResponse
from slack_cli_agent.review.base import ReviewTarget, ReviewTask, as_table, cell, model_effort_cell
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
        self.calls: list[tuple[str, str, bool]] = []

    def run(self, prompt: str, session_id: str, resume: bool) -> EngineResponse:
        self.calls.append((prompt, session_id, resume))
        return self._responses.pop(0)


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

    def build_header(self, target: ReviewTarget, record: Mapping[str, Any] | None, link: str) -> str:
        return "head\n\n"


@dataclass
class Rig:
    ledger: ReviewLedger
    reactions: FakeReactions
    publisher: FakePublisher
    engine: FakeEngine
    message_lookup: FakeMessageLookup
    task: ReviewTask


_MISSING = object()


def make_rig(database, *, msg=_MISSING, engine_responses=None, record=None) -> Rig:
    ledger = ReviewLedger(database)
    reactions = FakeReactions()
    publisher = FakePublisher()
    engine = FakeEngine(engine_responses or [_ok_response("요약===상세===상세내용")])
    message_lookup = FakeMessageLookup(
        {"thread_ts": "100.0", "text": "지목한 답변"} if msg is _MISSING else msg
    )
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
    )
    return Rig(ledger, reactions, publisher, engine, message_lookup, task)


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
        rec = rig.ledger.find("fake_kind", "C1", "1.1")
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
