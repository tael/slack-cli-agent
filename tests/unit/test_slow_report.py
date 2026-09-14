"""observability/slow_report.py 의 느린 요청 시간 분해·발신 테스트.

원본 bot.py 의 `time_breakdown()`(3227행 근처), `diagnose_elapsed()`(3142행),
`report_slow()`(3467행)를 옮긴 것이다. 이관 대상 세부는 다음과 같다.

- 도구 실행 구간(tool_use 직후 tool_result 까지)은 정확히 계산돼야 한다
- 사고와 단순 대기는 출력 토큰 수 기준으로 갈린다. 토큰 수를 모르면 전부
  단순 대기다
- `since_ts` 이전 이벤트는 계산에서 제외된다. 원본 docstring 의 실측 사례
  (세 번째 요청이 실제로는 409초였는데 since_ts 없이 계산하면 920.8초로
  잡혔다)를 재현하는 형태로 검증한다
- 소요가 기준값 미만이면, 보고 채널이 비어 있으면 보고하지 않는다
- 세션 기록이 없거나 깨져도 예외를 밖으로 내지 않는다
- 표 형식과 "근사" 표기
"""

from __future__ import annotations

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.engine.base import Usage
from slack_cli_agent.engine.transcript import SessionTranscriptReader, TranscriptEvent
from slack_cli_agent.observability.slow_report import (
    ElapsedDiagnostician,
    SlowReportFormatter,
    SlowRequestMeta,
    SessionContext,
    SessionContextCalculator,
    SlowRequestReporter,
    TimeBreakdown,
    TimeBreakdownCalculator,
    UsageRowBuilder,
    tail_output,
)


class FakeTranscriptReader(SessionTranscriptReader):
    """미리 정해 둔 이벤트 목록을 그대로 돌려주는 대역."""

    def __init__(self, events: list[TranscriptEvent] | None = None, broken: bool = False) -> None:
        self._events = events or []
        self._broken = broken

    def read(self, session_id: str) -> list[TranscriptEvent]:
        if self._broken:
            return []
        return list(self._events)


def make_settings(**overrides) -> RuntimeSettings:
    return RuntimeSettings(**overrides)


class Test도구실행구간계산:
    def test_tool_use_다음_tool_result까지가_도구실행이다(self) -> None:
        events = [
            TranscriptEvent(ts=0.0, role="assistant", kind="tool_use", brief="Bash ls", output_tokens=None),
            TranscriptEvent(ts=5.0, role="user", kind="tool_result", brief="", output_tokens=None),
        ]
        calc = TimeBreakdownCalculator(FakeTranscriptReader(events), assumed_tokens_per_sec=40)
        result = calc.compute("s1")
        assert result is not None
        assert result.tool_sec == 5.0
        assert result.think_sec == 0.0
        assert result.wait_sec == 0.0
        assert result.start_ts == 0.0
        assert result.end_ts == 5.0


class Test사고와단순대기구분:
    def test_토큰수만큼_사고시간으로_잡고_나머지는_단순대기다(self) -> None:
        # 직전 이벤트(사용자 tool_result, 시각 0) 다음 assistant 턴이 시각 10에
        # 출력 토큰 40개를 냈다. 초당 40개 가정이면 사고로 설명되는 시간은
        # 1초이고, 나머지 9초는 토큰 생성과 무관한 단순 대기다.
        events = [
            TranscriptEvent(ts=0.0, role="user", kind="tool_result", brief="", output_tokens=None),
            TranscriptEvent(ts=10.0, role="assistant", kind="text", brief="", output_tokens=40),
        ]
        calc = TimeBreakdownCalculator(FakeTranscriptReader(events), assumed_tokens_per_sec=40)
        result = calc.compute("s1")
        assert result is not None
        assert result.think_sec == 1.0
        assert result.wait_sec == 9.0

    def test_토큰수를_모르면_전부_단순대기다(self) -> None:
        events = [
            TranscriptEvent(ts=0.0, role="user", kind="tool_result", brief="", output_tokens=None),
            TranscriptEvent(ts=7.0, role="assistant", kind="text", brief="", output_tokens=None),
        ]
        calc = TimeBreakdownCalculator(FakeTranscriptReader(events), assumed_tokens_per_sec=40)
        result = calc.compute("s1")
        assert result is not None
        assert result.think_sec == 0.0
        assert result.wait_sec == 7.0


class TestSinceTs로_이전요청_제외:
    def test_since_ts_이전_이벤트를_끊어야_이번_요청_구간만_남는다(self) -> None:
        """원본 docstring 실측 사례 재현.

        세 번째 요청이 실제로는 409초였는데, since_ts 없이 파일 전체
        (앞선 두 요청 + 그 사이 사용자 응답 대기)를 잡으면 920.8초로
        잘못 계산됐다. since_ts 를 주면 이번 요청 시작 이전 이벤트가
        제외돼 실제 구간(409초)만 남아야 한다.
        """
        # 앞선 두 요청 + 대기. 전체 구간은 0부터 920.8까지다.
        earlier_events = [
            TranscriptEvent(ts=0.0, role="user", kind=None, brief="", output_tokens=None),
            TranscriptEvent(ts=100.0, role="assistant", kind="text", brief="", output_tokens=None),
            TranscriptEvent(ts=511.8, role="user", kind=None, brief="", output_tokens=None),
        ]
        # 이번(세 번째) 요청은 511.8부터 시작해 409초 뒤인 920.8에 끝난다.
        this_request_start = 511.8
        this_request_events = [
            TranscriptEvent(ts=this_request_start, role="user", kind=None, brief="", output_tokens=None),
            TranscriptEvent(ts=920.8, role="assistant", kind="text", brief="", output_tokens=None),
        ]
        events = earlier_events + this_request_events[1:]
        calc = TimeBreakdownCalculator(FakeTranscriptReader(events), assumed_tokens_per_sec=40)

        without_since = calc.compute("s1")
        assert without_since is not None
        assert round(without_since.end_ts - without_since.start_ts, 1) == 920.8

        with_since = calc.compute("s1", since_ts=this_request_start)
        assert with_since is not None
        assert round(with_since.end_ts - with_since.start_ts, 1) == 409.0


class Test세션기록없거나깨짐:
    def test_이벤트가_없으면_None이다(self) -> None:
        calc = TimeBreakdownCalculator(FakeTranscriptReader([]), assumed_tokens_per_sec=40)
        assert calc.compute("없는세션") is None

    def test_이벤트가_하나뿐이면_None이다(self) -> None:
        events = [TranscriptEvent(ts=0.0, role="user", kind=None, brief="", output_tokens=None)]
        calc = TimeBreakdownCalculator(FakeTranscriptReader(events), assumed_tokens_per_sec=40)
        assert calc.compute("s1") is None

    def test_파서가_예외_없이_빈값을_주면_계산도_예외없이_None이다(self) -> None:
        calc = TimeBreakdownCalculator(FakeTranscriptReader(broken=True), assumed_tokens_per_sec=40)
        assert calc.compute("s1") is None


class TestElapsedDiagnostician:
    def test_monotonic값이_없으면_판단불가다(self) -> None:
        diag = ElapsedDiagnostician(sleep_gap_suspect_sec=30)
        result = diag.diagnose(elapsed=100.0, mono_elapsed=None)
        assert "판단 불가" in result.diagnosis

    def test_공백이_기준을_넘으면_공백의심이다(self) -> None:
        diag = ElapsedDiagnostician(sleep_gap_suspect_sec=30)
        result = diag.diagnose(elapsed=2580.0, mono_elapsed=42.0)
        assert "공백" in result.diagnosis
        assert "42" in result.elapsed_desc

    def test_공백이_기준_미만이면_실제_처리다(self) -> None:
        diag = ElapsedDiagnostician(sleep_gap_suspect_sec=30)
        result = diag.diagnose(elapsed=100.0, mono_elapsed=95.0)
        assert result.diagnosis == "실제 처리에 시간이 걸렸습니다"


class TestSlowReportFormatter:
    def test_요약에_근사_표기가_들어간다(self) -> None:
        formatter = SlowReportFormatter(assumed_tokens_per_sec=40)
        breakdown = TimeBreakdown(
            start_ts=0.0, end_ts=10.0, tool_sec=2.0, think_sec=3.0, wait_sec=5.0, top_gaps=(),
        )
        meta = SlowRequestMeta(
            elapsed_wall=800.0, mono_elapsed=790.0, started=0.0, model="claude-x",
            model_actual=None, effort="high", num_turns=3, reason=None,
            session_id="세션1", resume=True, channel="C1", channel_name="테스트채널", text="원문",
        )
        diagnosis = ElapsedDiagnostician(sleep_gap_suspect_sec=30).diagnose(800.0, 790.0)
        summary, detail = formatter.format(meta, diagnosis, breakdown)
        assert "근사" in detail
        assert "세션1" in detail or "세션1"[:8] in detail
        assert "800" in summary

    def test_계산결과가_없으면_계산못했다고_적는다(self) -> None:
        formatter = SlowReportFormatter(assumed_tokens_per_sec=40)
        meta = SlowRequestMeta(
            elapsed_wall=900.0, mono_elapsed=None, started=None, model="claude-x",
            model_actual=None, effort="low", num_turns=None, reason="timeout",
            session_id="세션2", resume=False, channel="C1", channel_name="테스트채널", text="원문",
        )
        diagnosis = ElapsedDiagnostician(sleep_gap_suspect_sec=30).diagnose(900.0, None)
        summary, detail = formatter.format(meta, diagnosis, None)
        assert "계산하지 못했습니다" in detail


class FakePublisher:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str | None, str, bool]] = []
        self._next_ts = 1

    def post(self, channel: str, thread_ts, text: str, rich: bool) -> str | None:
        self.posts.append((channel, thread_ts, text, rich))
        ts = f"posted-{self._next_ts}"
        self._next_ts += 1
        return ts


class TestSlowRequestReporter:
    def _make_reporter(self, publisher, troubleshoot_channel="TS", events=None):
        settings = make_settings(slow_report_sec=800, sleep_gap_suspect_sec=30, assumed_tokens_per_sec=40)
        calc = TimeBreakdownCalculator(FakeTranscriptReader(events or []), settings.assumed_tokens_per_sec)
        diagnostician = ElapsedDiagnostician(settings.sleep_gap_suspect_sec)
        formatter = SlowReportFormatter(settings.assumed_tokens_per_sec)
        return SlowRequestReporter(
            publisher=publisher, calculator=calc, diagnostician=diagnostician,
            formatter=formatter, settings=settings, troubleshoot_channel=troubleshoot_channel,
        )

    def _meta(self, **overrides) -> SlowRequestMeta:
        base = dict(
            elapsed_wall=850.0, mono_elapsed=840.0, started=0.0, model="claude-x",
            model_actual=None, effort="high", num_turns=5, reason=None,
            session_id="s1", resume=True, channel="C1", channel_name="테스트채널", text="원문",
        )
        base.update(overrides)
        return SlowRequestMeta(**base)

    def test_기준값_미만이면_보고하지_않는다(self) -> None:
        publisher = FakePublisher()
        reporter = self._make_reporter(publisher)
        result = reporter.maybe_report(self._meta(elapsed_wall=799.0))
        assert result is None
        assert publisher.posts == []

    def test_보고채널이_비어있으면_보고하지_않는다(self) -> None:
        publisher = FakePublisher()
        reporter = self._make_reporter(publisher, troubleshoot_channel="")
        result = reporter.maybe_report(self._meta())
        assert result is None
        assert publisher.posts == []

    def test_기준값_이상이고_채널이_있으면_요약과_상세를_스레드로_올린다(self) -> None:
        publisher = FakePublisher()
        events = [
            TranscriptEvent(ts=0.0, role="user", kind=None, brief="", output_tokens=None),
            TranscriptEvent(ts=850.0, role="assistant", kind="text", brief="", output_tokens=None),
        ]
        reporter = self._make_reporter(publisher, events=events)
        result = reporter.maybe_report(self._meta())
        assert result == "posted-1"
        assert len(publisher.posts) == 2
        summary_call, detail_call = publisher.posts
        assert summary_call[0] == "TS"
        assert summary_call[1] is None
        assert detail_call[1] == "posted-1"

    def test_세션기록이_없어도_예외없이_요약만_올린다(self) -> None:
        publisher = FakePublisher()
        reporter = self._make_reporter(publisher, events=[])
        result = reporter.maybe_report(self._meta(session_id="없는세션"))
        assert result == "posted-1"
        assert len(publisher.posts) == 2

    def test_게시자체가_예외를_내도_밖으로_내지않는다(self) -> None:
        class BoomPublisher:
            def post(self, *args, **kwargs):
                raise RuntimeError("슬랙 실패")

        reporter = self._make_reporter(BoomPublisher())
        result = reporter.maybe_report(self._meta())
        assert result is None


class Test재시도감지:
    """원본 `detect_retries()`(bot.py 3196행)의 역산 규칙 이관 검증.

    끊겼다 다시 부른 요청은 실패 이벤트 자체가 기록에 안 남는다. 다시 부른
    요청은 캐시 기록이 0인데 캐시 읽기가 직전 요청의 (읽기+기록) 합보다
    커진다. 그 차이만큼을 끊긴 시도가 남긴 몫으로 본다.
    """

    def test_재시도가_있으면_그_구간의_대기를_재시도로_잡는다(self) -> None:
        events = [
            TranscriptEvent(ts=0.0, role="user", kind=None, brief="", output_tokens=None),
            TranscriptEvent(
                ts=5.0, role="assistant", kind="text", brief="", output_tokens=None,
                cache_creation_tokens=100, cache_read_tokens=50, request_id="req-1",
            ),
            TranscriptEvent(
                ts=20.0, role="assistant", kind="text", brief="", output_tokens=None,
                cache_creation_tokens=0, cache_read_tokens=200, request_id="req-2",
            ),
        ]
        calc = TimeBreakdownCalculator(FakeTranscriptReader(events), assumed_tokens_per_sec=40)
        result = calc.compute("s1")
        assert result is not None
        # 두 번째 요청(ts=20)이 재시도다. orphan = 200 - (50 + 100) = 50
        assert result.retries == {20.0: 50}
        assert result.retry_sec == 15.0  # ts5→ts20 구간(15초)이 재시도로 버린 시간
        assert result.wait_sec == 5.0  # ts0→ts5 구간(5초)만 순수 단순 대기
        retry_gap = next(gap for gap in result.top_gaps if gap.end_ts == 20.0)
        assert retry_gap.retry_orphan_tokens == 50

    def test_재시도가_없으면_전부_단순대기로_잡힌다(self) -> None:
        events = [
            TranscriptEvent(ts=0.0, role="user", kind=None, brief="", output_tokens=None),
            TranscriptEvent(
                ts=5.0, role="assistant", kind="text", brief="", output_tokens=None,
                cache_creation_tokens=100, cache_read_tokens=50, request_id="req-1",
            ),
            TranscriptEvent(
                ts=20.0, role="assistant", kind="text", brief="", output_tokens=None,
                cache_creation_tokens=80, cache_read_tokens=60, request_id="req-2",
            ),
        ]
        calc = TimeBreakdownCalculator(FakeTranscriptReader(events), assumed_tokens_per_sec=40)
        result = calc.compute("s1")
        assert result is not None
        assert result.retries == {}
        assert result.retry_sec == 0.0
        assert result.wait_sec == 20.0
        assert all(gap.retry_orphan_tokens is None for gap in result.top_gaps)


class TestSlowReportFormatter재시도표시:
    def test_재시도가_있으면_전용_구획과_판정열이_나온다(self) -> None:
        formatter = SlowReportFormatter(assumed_tokens_per_sec=40)
        from slack_cli_agent.observability.slow_report import GapDetail

        breakdown = TimeBreakdown(
            start_ts=0.0, end_ts=20.0, tool_sec=0.0, think_sec=0.0, wait_sec=5.0,
            top_gaps=(
                GapDetail(duration_sec=15.0, start_ts=5.0, end_ts=20.0, last_tool_brief="",
                          output_tokens=None, think_sec=0.0, wait_sec=15.0, retry_orphan_tokens=50),
            ),
            retry_sec=15.0, retries={20.0: 50},
        )
        meta = SlowRequestMeta(
            elapsed_wall=800.0, mono_elapsed=790.0, started=0.0, model="claude-x",
            model_actual=None, effort="high", num_turns=3, reason=None,
            session_id="세션1", resume=True, channel="C1", channel_name="테스트채널", text="원문",
        )
        diagnosis = ElapsedDiagnostician(sleep_gap_suspect_sec=30).diagnose(800.0, 790.0)
        _, detail = formatter.format(meta, diagnosis, breakdown)
        assert "끊겼다 다시 부른 요청" in detail
        assert "재시도" in detail

    def test_재시도가_없으면_전용_구획이_안_나온다(self) -> None:
        formatter = SlowReportFormatter(assumed_tokens_per_sec=40)
        breakdown = TimeBreakdown(
            start_ts=0.0, end_ts=20.0, tool_sec=0.0, think_sec=0.0, wait_sec=20.0, top_gaps=(),
        )
        meta = SlowRequestMeta(
            elapsed_wall=800.0, mono_elapsed=790.0, started=0.0, model="claude-x",
            model_actual=None, effort="high", num_turns=3, reason=None,
            session_id="세션1", resume=True, channel="C1", channel_name="테스트채널", text="원문",
        )
        diagnosis = ElapsedDiagnostician(sleep_gap_suspect_sec=30).diagnose(800.0, 790.0)
        _, detail = formatter.format(meta, diagnosis, breakdown)
        assert "끊겼다 다시 부른 요청" not in detail


class Test표준출력오류꼬리첨부:
    def test_긴_버퍼는_800자만_남는다(self) -> None:
        buf = "x" * 1000 + "끝부분"
        result = tail_output(buf)
        assert len(result) == 800
        assert result.endswith("끝부분")

    def test_바이트버퍼도_문자열로_바꿔_자른다(self) -> None:
        buf = ("y" * 1000 + "끝").encode("utf-8")
        result = tail_output(buf)
        assert len(result) == 800

    def test_빈값이면_빈문자열이다(self) -> None:
        assert tail_output(None) == ""
        assert tail_output("") == ""

    def test_값이_있으면_보고에_나오고_600자로_잘린다(self) -> None:
        formatter = SlowReportFormatter(assumed_tokens_per_sec=40)
        breakdown = TimeBreakdown(start_ts=0.0, end_ts=10.0, tool_sec=0.0, think_sec=0.0, wait_sec=10.0)
        meta = SlowRequestMeta(
            elapsed_wall=800.0, mono_elapsed=790.0, started=0.0, model="claude-x",
            model_actual=None, effort="high", num_turns=None, reason="nonzero_exit",
            session_id="세션1", resume=True, channel="C1", channel_name="테스트채널", text="원문",
            stdout_tail="a" * 700 + "표시부분", stderr_tail=None,
        )
        diagnosis = ElapsedDiagnostician(sleep_gap_suspect_sec=30).diagnose(800.0, 790.0)
        _, detail = formatter.format(meta, diagnosis, breakdown)
        assert "표준 출력 끝부분" in detail
        assert "표시부분" in detail
        assert "표준 오류 끝부분" not in detail

    def test_값이_없으면_블록이_안_나온다(self) -> None:
        formatter = SlowReportFormatter(assumed_tokens_per_sec=40)
        breakdown = TimeBreakdown(start_ts=0.0, end_ts=10.0, tool_sec=0.0, think_sec=0.0, wait_sec=10.0)
        meta = SlowRequestMeta(
            elapsed_wall=800.0, mono_elapsed=790.0, started=0.0, model="claude-x",
            model_actual=None, effort="high", num_turns=None, reason=None,
            session_id="세션1", resume=True, channel="C1", channel_name="테스트채널", text="원문",
        )
        diagnosis = ElapsedDiagnostician(sleep_gap_suspect_sec=30).diagnose(800.0, 790.0)
        _, detail = formatter.format(meta, diagnosis, breakdown)
        assert "끝부분" not in detail


class TestUsageRowBuilder:
    """원본 `usage_rows()`(bot.py 3420행)의 채널 게이트 이관 검증.

    토큰 사용 현황은 민감정보라 보고 채널이 소유자 전용 채널일 때만 낸다.
    """

    def test_소유자_전용_채널이면_사용량_행이_나온다(self) -> None:
        builder = UsageRowBuilder(owner_only_channels=frozenset({"TS"}))
        usage = Usage(input_tokens=100, output_tokens=50, cache_creation_tokens=10, cache_read_tokens=20)
        rows = builder.build(usage, troubleshoot_channel="TS")
        assert len(rows) == 1
        assert rows[0][0] == "토큰"
        assert "180" in rows[0][1]

    def test_소유자_전용_채널이_아니면_아무것도_안_낸다(self) -> None:
        builder = UsageRowBuilder(owner_only_channels=frozenset({"TS"}))
        usage = Usage(input_tokens=100, output_tokens=50, cache_creation_tokens=10, cache_read_tokens=20)
        rows = builder.build(usage, troubleshoot_channel="다른채널")
        assert rows == []

    def test_소유자_전용_채널인데_사용량_기록이_없으면_사유를_낸다(self) -> None:
        builder = UsageRowBuilder(owner_only_channels=frozenset({"TS"}))
        rows = builder.build(None, troubleshoot_channel="TS")
        assert len(rows) == 1
        assert "기록 없음" in rows[0][1]


class TestSlowReportFormatter사용량행:
    def test_사용량_행이_있으면_요약에_들어간다(self) -> None:
        formatter = SlowReportFormatter(assumed_tokens_per_sec=40)
        breakdown = TimeBreakdown(start_ts=0.0, end_ts=10.0, tool_sec=0.0, think_sec=0.0, wait_sec=10.0)
        meta = SlowRequestMeta(
            elapsed_wall=800.0, mono_elapsed=790.0, started=0.0, model="claude-x",
            model_actual=None, effort="high", num_turns=None, reason=None,
            session_id="세션1", resume=True, channel="C1", channel_name="테스트채널", text="원문",
        )
        diagnosis = ElapsedDiagnostician(sleep_gap_suspect_sec=30).diagnose(800.0, 790.0)
        summary, _ = formatter.format(meta, diagnosis, breakdown, usage_rows=[("토큰", "180개")])
        assert "토큰" in summary
        assert "180개" in summary

    def test_사용량_행이_없으면_요약에_안_들어간다(self) -> None:
        formatter = SlowReportFormatter(assumed_tokens_per_sec=40)
        breakdown = TimeBreakdown(start_ts=0.0, end_ts=10.0, tool_sec=0.0, think_sec=0.0, wait_sec=10.0)
        meta = SlowRequestMeta(
            elapsed_wall=800.0, mono_elapsed=790.0, started=0.0, model="claude-x",
            model_actual=None, effort="high", num_turns=None, reason=None,
            session_id="세션1", resume=True, channel="C1", channel_name="테스트채널", text="원문",
        )
        diagnosis = ElapsedDiagnostician(sleep_gap_suspect_sec=30).diagnose(800.0, 790.0)
        summary, _ = formatter.format(meta, diagnosis, breakdown)
        assert "토큰" not in summary


class TestSessionContextCalculator:
    """원본 `session_context()`(bot.py 3384행) 이관 검증.

    그 세션이 지금 쓰는 컨텍스트 크기는 마지막 assistant 턴의 usage 합이다.
    없으면 다음 요청에서 컨텍스트가 부족해 막힐지를 보고에서 미리 확인할 수
    없다. 한도는 `RuntimeSettings.context_limit` 표에서 찾고, 표에 없는
    모델이면 한도가 None 이다 — 그때 호출하는 쪽은 비율을 내지 않는다.
    """

    def _event(self, ts: float, role: str, **tokens: int) -> TranscriptEvent:
        return TranscriptEvent(ts=ts, role=role, kind="text", brief="", output_tokens=tokens.get("output"),
                               input_tokens=tokens.get("input"),
                               cache_creation_tokens=tokens.get("creation"),
                               cache_read_tokens=tokens.get("read"))

    def test_마지막_assistant_턴의_usage_합이_사용량이다(self) -> None:
        reader = FakeTranscriptReader([
            self._event(1.0, "assistant", input=1, output=2, creation=3, read=4),
            self._event(2.0, "assistant", input=10, output=20, creation=30, read=40),
        ])
        calculator = SessionContextCalculator(reader, context_limit={})
        assert calculator.compute("s1").used == 100

    def test_assistant가_아닌_턴은_세지_않는다(self) -> None:
        reader = FakeTranscriptReader([
            self._event(1.0, "assistant", input=1, output=2, creation=3, read=4),
            self._event(2.0, "user", input=999),
        ])
        calculator = SessionContextCalculator(reader, context_limit={})
        assert calculator.compute("s1").used == 10

    def test_usage가_없는_assistant_턴은_건너뛴다(self) -> None:
        reader = FakeTranscriptReader([
            self._event(1.0, "assistant", input=1, output=2, creation=3, read=4),
            self._event(2.0, "assistant"),
        ])
        calculator = SessionContextCalculator(reader, context_limit={})
        assert calculator.compute("s1").used == 10

    def test_기록이_없으면_사용량이_None이다(self) -> None:
        calculator = SessionContextCalculator(FakeTranscriptReader([]), context_limit={})
        assert calculator.compute("s1").used is None

    def test_표에_있는_모델이면_한도를_함께_돌려준다(self) -> None:
        calculator = SessionContextCalculator(FakeTranscriptReader([]), context_limit={"m": 200000})
        assert calculator.compute("s1", model="m").limit == 200000

    def test_표에_없는_모델이면_한도가_None이다(self) -> None:
        calculator = SessionContextCalculator(FakeTranscriptReader([]), context_limit={"m": 200000})
        assert calculator.compute("s1", model="다른모델").limit is None


class TestUsageRowBuilder세션행:
    """원본 `usage_rows()` 의 "세션" 행 이관 검증.

    토큰 행과 같은 채널 조건 아래 있다. 계산기를 안 주면 세션 행 자체를
    내지 않는다 — 세션 기록 경로가 없는 조립 코드에서 잘못된 값을 내는 것보다
    행이 없는 편이 낫다.
    """

    def _builder(self, used: int | None, limit: int | None) -> "UsageRowBuilder":
        class 고정계산기:
            def compute(self, session_id: str, model: str | None = None) -> SessionContext:
                return SessionContext(used=used, limit=limit)

        return UsageRowBuilder(owner_only_channels=frozenset({"TS"}), session_context=고정계산기())

    def test_한도를_알면_비율을_적는다(self) -> None:
        rows = self._builder(100_000, 200_000).build(None, "TS", session_id="s1", model="m")
        assert rows[1][0] == "세션"
        assert "100k/200k" in rows[1][1]
        assert "50퍼센트" in rows[1][1]

    def test_한도를_모르면_비율을_안_적는다(self) -> None:
        rows = self._builder(100_000, None).build(None, "TS", session_id="s1", model="m")
        assert "퍼센트" not in rows[1][1]
        assert "한도 미상" in rows[1][1]

    def test_사용량을_못_읽으면_사유를_적는다(self) -> None:
        rows = self._builder(None, 200_000).build(None, "TS", session_id="s1", model="m")
        assert "조회 실패" in rows[1][1]

    def test_소유자_전용_채널이_아니면_세션_행도_안_낸다(self) -> None:
        rows = self._builder(100_000, 200_000).build(None, "다른채널", session_id="s1", model="m")
        assert rows == []

    def test_계산기가_없으면_세션_행이_없다(self) -> None:
        builder = UsageRowBuilder(owner_only_channels=frozenset({"TS"}))
        rows = builder.build(None, "TS", session_id="s1", model="m")
        assert [row[0] for row in rows] == ["토큰"]


class TestSlowReportFormatter발생시각:
    """원본 `report_slow()` 의 rows 첫 항목인 "발생" 행 이관 검증.

    이관본에서 빠져 있었다. 보고가 언제 일어난 요청에 대한 것인지 표에서
    바로 확인되지 않으면, 채널 목록의 여러 보고를 대조할 때 슬랙 게시
    시각으로만 추정하게 된다.
    """

    def _meta(self, started: float | None) -> SlowRequestMeta:
        return SlowRequestMeta(
            elapsed_wall=900.0, mono_elapsed=900.0, started=started, model="m", model_actual=None,
            effort="high", num_turns=1, reason=None, session_id="s1" * 8, resume=False,
            channel="C1", channel_name="대화방", text="요청",
        )

    def test_요청_시작_시각을_KST로_적는다(self) -> None:
        formatter = SlowReportFormatter(assumed_tokens_per_sec=40)
        diagnosis = ElapsedDiagnostician(sleep_gap_suspect_sec=30).diagnose(900.0, 900.0)
        # 2026-09-01 09:00:00 KST
        summary, _ = formatter.format(self._meta(1788480000.0), diagnosis, None)
        assert "| 발생 | 09:00:00 KST |" in summary

    def test_시작_시각이_없으면_보고_시각을_적는다(self) -> None:
        formatter = SlowReportFormatter(assumed_tokens_per_sec=40, now=lambda: 1788480000.0)
        diagnosis = ElapsedDiagnostician(sleep_gap_suspect_sec=30).diagnose(900.0, 900.0)
        summary, _ = formatter.format(self._meta(None), diagnosis, None)
        assert "| 발생 | 09:00:00 KST |" in summary

    def test_발생_행이_표의_첫_줄이다(self) -> None:
        formatter = SlowReportFormatter(assumed_tokens_per_sec=40)
        diagnosis = ElapsedDiagnostician(sleep_gap_suspect_sec=30).diagnose(900.0, 900.0)
        summary, _ = formatter.format(self._meta(1788480000.0), diagnosis, None)
        # rows[0] 은 표 머리다. 구분선은 "|---" 로 시작해 이 목록에 안 들어온다.
        rows = [line for line in summary.splitlines() if line.startswith("| ")]
        assert rows[1].startswith("| 발생 |")
