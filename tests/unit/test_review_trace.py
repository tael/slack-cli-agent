"""review/trace.py 의 DebugTraceTask 테스트.

프롬프트·머리말 문구는 원본 `_run_debug_trace()` 의 리터럴을 그대로 옮겼다.
원본은 요청자 이름을 문자열로 박아 뒀다 — 조직 고유값이라 새 코드베이스에
넣을 수 없어 `owner_display_name` 생성자 인자로 뺐다(이 작업의 명시적 지침).
"""

from __future__ import annotations

from slack_cli_agent.review.base import ReviewTarget
from slack_cli_agent.review.trace import DebugTraceTask


def make_task(**overrides) -> DebugTraceTask:
    kwargs = {
        "ledger": None,
        "message_lookup": None,
        "transcript": None,
        "answer_finder": None,
        "reactions": None,
        "permalinks": None,
        "publisher": None,
        "engine": None,
        "troubleshoot_channel": "TS",
        "bot_display_name": "테스트봇",
        "code_dir": "/code",
        "persona_dir": "/persona",
        "owner_display_name": "관리자",
    }
    kwargs.update(overrides)
    return DebugTraceTask(**kwargs)


class Test클래스변수:
    def test_log_name은debug_trace이다(self) -> None:
        assert DebugTraceTask.log_name == "debug_trace"

    def test_구분선없어도재시도하지않는다(self) -> None:
        assert make_task().retry_on_missing_split() is False


class Test프롬프트:
    def test_요청자이름은조직값이아니라주입값을쓴다(self) -> None:
        task = make_task(owner_display_name="관리자")
        target = ReviewTarget(channel="C1", ts="1.1", by_user="U1", channel_name="회의방", rich=True)
        prompt = task.build_prompt(target, transcript="[대화록]", flagged="답변", question="")
        assert "관리자" in prompt
        assert "잘못됐다는 지적이 아니다" in prompt
        assert "테스트봇" in prompt

    def test_그답을부른요청이없으면그절을안넣는다(self) -> None:
        task = make_task()
        target = ReviewTarget(channel="C1", ts="1.1", by_user="U1", channel_name="회의방", rich=True)
        prompt = task.build_prompt(target, transcript="[대화록]", flagged="답변", question="")
        assert "그 답을 부른 요청" not in prompt


class Test머리말:
    def test_요청한사람과대상답변을보인다(self) -> None:
        task = make_task()
        target = ReviewTarget(channel="C1", ts="1.1", by_user="U1", channel_name="회의방", rich=True)
        header = task.build_header(target, None, "https://slack/x")
        assert "## 디버그 : 회의방" in header
        assert "<@U1>" in header
        assert "https://slack/x" in header

    def test_링크가없으면그줄을뺀다(self) -> None:
        task = make_task()
        target = ReviewTarget(channel="C1", ts="1.1", by_user="U1", channel_name="회의방", rich=True)
        header = task.build_header(target, None, "")
        assert "대상 답변" not in header


class _FakeMessageLookup:
    def __init__(self, msg) -> None:
        self._msg = msg

    def find(self, channel: str, ts: str):
        return self._msg


class _FakeTranscript:
    def transcript(self, channel: str, thread_ts: str) -> str:
        return "[대화록]"


class _FakeAnswerFinder:
    def find(self, channel: str, thread_ts: str, text: str):
        return None


class _FakeReactions:
    def mark_processing(self, channel: str, ts: str) -> None:
        pass

    def clear_processing(self, channel: str, ts: str) -> None:
        pass


class _FakePermalinks:
    def permalink(self, channel: str, ts: str) -> str:
        return f"https://slack.example/{channel}/{ts}"


class _FakePublisher:
    def __init__(self) -> None:
        self.posts: list = []

    def post(self, channel: str, thread_ts, text: str, *, rich: bool):
        self.posts.append((channel, thread_ts, text))
        return "parent.1" if thread_ts is None else None


class _FakeEngine:
    def __init__(self) -> None:
        self.calls: list = []

    def run(self, prompt: str, session_id: str | None, resume: bool, progress_log=None):
        from slack_cli_agent.engine.base import EngineResponse

        self.calls.append(prompt)
        return EngineResponse(
            ok=True,
            body="흐름 요약",
            session_id=session_id,
            model_actual=None,
            elapsed=0.0,
            turns=1,
            usage=None,
        )


class Test중복방지:
    """원본 load_debug_traces()/save_debug_trace() 가 막던 것 — 이미 흐름을
    낸 답변에 다시 흐름을 내지 않는다.

    새 코드베이스에서는 이 중복 방지를 ReviewLedger + ReviewTask.run() 이
    이미 일반화해 담당한다. DebugTraceTask 는 log_name="debug_trace" 로
    그 구조에 맞춰 붙기만 하면 된다 — 새 저장 계층을 만들지 않는다.
    """

    def test_이미_흐름을_낸_답변은_다시_내지_않는다(self, database) -> None:
        from slack_cli_agent.review.ledger import ReviewLedger

        ledger = ReviewLedger(database)
        ledger.complete("debug_trace", "C1", "1.1", by="U1")
        engine = _FakeEngine()
        task = make_task(
            ledger=ledger,
            message_lookup=_FakeMessageLookup({"text": "답변", "ts": "1.1"}),
            transcript=_FakeTranscript(),
            answer_finder=_FakeAnswerFinder(),
            reactions=_FakeReactions(),
            permalinks=_FakePermalinks(),
            publisher=_FakePublisher(),
            engine=engine,
        )
        target = ReviewTarget(channel="C1", ts="1.1", by_user="U2", channel_name="채널", rich=True)
        task.run(target)
        assert engine.calls == []

    def test_처음_요청한_흐름은_원장에_완료로_남는다(self, database) -> None:
        from slack_cli_agent.review.ledger import ReviewLedger

        ledger = ReviewLedger(database)
        engine = _FakeEngine()
        task = make_task(
            ledger=ledger,
            message_lookup=_FakeMessageLookup({"text": "답변", "ts": "1.1"}),
            transcript=_FakeTranscript(),
            answer_finder=_FakeAnswerFinder(),
            reactions=_FakeReactions(),
            permalinks=_FakePermalinks(),
            publisher=_FakePublisher(),
            engine=engine,
        )
        target = ReviewTarget(channel="C1", ts="1.1", by_user="U2", channel_name="채널", rich=True)
        task.run(target)
        assert len(engine.calls) == 1
        rec = ledger.find("debug_trace", "C1", "1.1")
        assert rec.status == "완료"
