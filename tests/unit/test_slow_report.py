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

import logging
from dataclasses import fields
from typing import Any, ClassVar

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.engine.base import Usage
from slack_cli_agent.engine.transcript import SessionTranscriptReader, TranscriptEvent
from slack_cli_agent.observability.slow_report import (
    ElapsedDiagnostician,
    SessionContext,
    SessionContextCalculator,
    SlowReportFormatter,
    SlowRequestMeta,
    SlowRequestReporter,
    TimeBreakdown,
    TimeBreakdownCalculator,
    UsageRowBuilder,
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
        reader = FakeTranscriptReader(events)
        calc = TimeBreakdownCalculator(assumed_tokens_per_sec=40)
        result = calc.compute(reader, "s1")
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
        reader = FakeTranscriptReader(events)
        calc = TimeBreakdownCalculator(assumed_tokens_per_sec=40)
        result = calc.compute(reader, "s1")
        assert result is not None
        assert result.think_sec == 1.0
        assert result.wait_sec == 9.0

    def test_토큰수를_모르면_전부_단순대기다(self) -> None:
        events = [
            TranscriptEvent(ts=0.0, role="user", kind="tool_result", brief="", output_tokens=None),
            TranscriptEvent(ts=7.0, role="assistant", kind="text", brief="", output_tokens=None),
        ]
        reader = FakeTranscriptReader(events)
        calc = TimeBreakdownCalculator(assumed_tokens_per_sec=40)
        result = calc.compute(reader, "s1")
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
        reader = FakeTranscriptReader(events)
        calc = TimeBreakdownCalculator(assumed_tokens_per_sec=40)

        without_since = calc.compute(reader, "s1")
        assert without_since is not None
        assert round(without_since.end_ts - without_since.start_ts, 1) == 920.8

        with_since = calc.compute(reader, "s1", since_ts=this_request_start)
        assert with_since is not None
        assert round(with_since.end_ts - with_since.start_ts, 1) == 409.0


class Test세션기록없거나깨짐:
    def test_이벤트가_없으면_None이다(self) -> None:
        reader = FakeTranscriptReader([])
        calc = TimeBreakdownCalculator(assumed_tokens_per_sec=40)
        assert calc.compute(reader, "없는세션") is None

    def test_이벤트가_하나뿐이면_None이다(self) -> None:
        events = [TranscriptEvent(ts=0.0, role="user", kind=None, brief="", output_tokens=None)]
        reader = FakeTranscriptReader(events)
        calc = TimeBreakdownCalculator(assumed_tokens_per_sec=40)
        assert calc.compute(reader, "s1") is None

    def test_파서가_예외_없이_빈값을_주면_계산도_예외없이_None이다(self) -> None:
        reader = FakeTranscriptReader(broken=True)
        calc = TimeBreakdownCalculator(assumed_tokens_per_sec=40)
        assert calc.compute(reader, "s1") is None


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

    def test_요청_행에서_슬랙_표기를_푼다(self) -> None:
        """슬랙 이벤트 API 는 원문을 이스케이프해 준다. 그대로 새 메시지에
        넣으면 그 글자가 그대로 보이고, 이미 렌더된 링크는 한 번 더 감싸여
        겹으로 남는다. 원본 bot.py:3510 도 clean_excerpt 를 거친다(sca-3o1)."""
        formatter = SlowReportFormatter(assumed_tokens_per_sec=40)
        meta = SlowRequestMeta(
            elapsed_wall=800.0, mono_elapsed=790.0, started=0.0, model="claude-x",
            model_actual=None, effort="high", num_turns=3, reason=None,
            session_id="세션1", resume=True, channel="C1", channel_name="테스트채널",
            text="&lt;태그&gt; 와 <https://y.com|라벨> 을 &amp; 로",
        )
        diagnosis = ElapsedDiagnostician(sleep_gap_suspect_sec=30).diagnose(800.0, 790.0)
        summary, _ = formatter.format(meta, diagnosis, None)
        # 남은 꺾쇠도 벗긴다. 원본 bot.py 의 clean_excerpt 가 그렇게 한다.
        assert "태그 와 라벨 을 & 로" in summary
        assert "&lt;" not in summary
        assert "https://y.com" not in summary

    def test_요청_행은_푼_뒤_이백자로_자른다(self) -> None:
        """이스케이프를 풀면 길이가 준다. 자르고 나서 풀면 200자 상한이
        원문 기준이 되어 결과가 그보다 짧아진다."""
        formatter = SlowReportFormatter(assumed_tokens_per_sec=40)
        meta = SlowRequestMeta(
            elapsed_wall=800.0, mono_elapsed=790.0, started=0.0, model="claude-x",
            model_actual=None, effort="high", num_turns=3, reason=None,
            session_id="세션1", resume=True, channel="C1", channel_name="테스트채널",
            text="&amp;" * 300,
        )
        diagnosis = ElapsedDiagnostician(sleep_gap_suspect_sec=30).diagnose(800.0, 790.0)
        summary, _ = formatter.format(meta, diagnosis, None)
        assert "&" * 200 in summary

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
        assert "900" in summary


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
    def _make_reporter(self, publisher, troubleshoot_channel="TS", events=None, owner_only=("TS",)):
        settings = make_settings(
            slow_report_sec=800,
            sleep_gap_suspect_sec=30,
            assumed_tokens_per_sec=40,
            owner_only_channels=frozenset(owner_only),
        )
        reader = FakeTranscriptReader(events or [])
        calc = TimeBreakdownCalculator(settings.assumed_tokens_per_sec)
        diagnostician = ElapsedDiagnostician(settings.sleep_gap_suspect_sec)
        formatter = SlowReportFormatter(settings.assumed_tokens_per_sec)
        return SlowRequestReporter(
            publisher=publisher, calculator=calc, diagnostician=diagnostician,
            formatter=formatter, settings=settings, troubleshoot_channel=troubleshoot_channel,
            readers=lambda engine: reader,
        )

    def _meta(self, **overrides) -> SlowRequestMeta:
        base: dict[str, Any] = {
            "elapsed_wall": 850.0, "mono_elapsed": 840.0, "started": 0.0, "model": "claude-x",
            "model_actual": None, "effort": "high", "num_turns": 5, "reason": None,
            "session_id": "s1", "resume": True, "channel": "C1", "channel_name": "테스트채널", "text": "원문",
        }
        base.update(overrides)
        return SlowRequestMeta(**base)

    def test_소유자_전용이_아닌_채널이면_아무것도_게시하지_않는다(self) -> None:
        """보고에는 원 채널 발화와 채널명과 세션 ID 가 들어간다. 소유자 전용이
        아닌 채널을 트러블슈팅 채널로 설정하면 그것이 그 채널 참여자 전원에게
        간다(sca-dh6). 지금까지는 토큰 행만 빠지고 보고는 나갔다."""
        publisher = FakePublisher()
        reporter = self._make_reporter(publisher, owner_only=())
        assert reporter.maybe_report(self._meta()) is None
        assert publisher.posts == []

    def test_다른_채널만_소유자_전용이어도_막는다(self) -> None:
        publisher = FakePublisher()
        reporter = self._make_reporter(publisher, owner_only=("OTHER",))
        assert reporter.maybe_report(self._meta()) is None
        assert publisher.posts == []

    def test_막혔으면_기동_시점에_경고를_남긴다(self, caplog) -> None:
        """게시 시점에만 막으면 설정 실수가 조용히 통과한다. 반영 후 보고
        0건을 고장으로 읽지 않으려면 기동 경고가 먼저 있어야 한다."""
        with caplog.at_level(logging.WARNING):
            self._make_reporter(FakePublisher(), owner_only=())
        assert any("소유자 전용" in r.getMessage() for r in caplog.records), caplog.records

    def test_채널이_비어_있으면_경고하지_않는다(self, caplog) -> None:
        """미설정은 유효한 설정이다. 경고를 내면 정상 구성에서 매번 뜬다."""
        with caplog.at_level(logging.WARNING):
            self._make_reporter(FakePublisher(), troubleshoot_channel="", owner_only=())
        assert caplog.records == []

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
        reader = FakeTranscriptReader(events)
        calc = TimeBreakdownCalculator(assumed_tokens_per_sec=40)
        result = calc.compute(reader, "s1")
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
        reader = FakeTranscriptReader(events)
        calc = TimeBreakdownCalculator(assumed_tokens_per_sec=40)
        result = calc.compute(reader, "s1")
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


class Test도구_시간을_못_가르는_기록:
    """제미나이 기록은 도구 호출과 결과가 한 step 이라 그 둘 사이가 기록에
    아예 없다. 그것을 0초로 적으면 "도구를 안 썼다" 로 읽힌다(sca-ebp)."""

    def _meta(self) -> SlowRequestMeta:
        return SlowRequestMeta(
            elapsed_wall=800.0, mono_elapsed=790.0, started=0.0, model="gemini-x",
            model_actual=None, effort="medium", num_turns=1, reason=None,
            session_id="세션1", resume=False, channel="C1", channel_name="테스트채널", text="원문",
        )

    def test_못_가르면_도구_실행_줄을_안_낸다(self) -> None:
        formatter = SlowReportFormatter(assumed_tokens_per_sec=40)
        breakdown = TimeBreakdown(
            start_ts=0.0, end_ts=20.0, tool_sec=0.0, think_sec=0.0, wait_sec=20.0,
            splits_tool_time=False,
        )
        diagnosis = ElapsedDiagnostician(sleep_gap_suspect_sec=30).diagnose(800.0, 790.0)
        _, detail = formatter.format(self._meta(), diagnosis, breakdown)

        assert "| 도구 실행 |" not in detail

    def test_못_가르면_그_사실을_적는다(self) -> None:
        formatter = SlowReportFormatter(assumed_tokens_per_sec=40)
        breakdown = TimeBreakdown(
            start_ts=0.0, end_ts=20.0, tool_sec=0.0, think_sec=0.0, wait_sec=20.0,
            splits_tool_time=False,
        )
        diagnosis = ElapsedDiagnostician(sleep_gap_suspect_sec=30).diagnose(800.0, 790.0)
        _, detail = formatter.format(self._meta(), diagnosis, breakdown)

        assert "도구 실행과 대기를 가를 수 없습니다" in detail

    def test_가를_수_있으면_전과_같다(self) -> None:
        formatter = SlowReportFormatter(assumed_tokens_per_sec=40)
        breakdown = TimeBreakdown(start_ts=0.0, end_ts=20.0, tool_sec=5.0, think_sec=0.0, wait_sec=15.0)
        diagnosis = ElapsedDiagnostician(sleep_gap_suspect_sec=30).diagnose(800.0, 790.0)
        _, detail = formatter.format(self._meta(), diagnosis, breakdown)

        assert "| 도구 실행 |" in detail
        assert "도구 실행과 대기를 가를 수 없습니다" not in detail


class Test출력_토큰을_안_남기는_기록:
    """제미나이는 step 에 출력 토큰을 안 남긴다. 그러면 사고 시간이 0 으로
    계산돼 전부 단순 대기로 밀리고, 보고가 "이 엔진은 토큰을 안 쓴다" 로
    읽힌다. 실측 2026-09-19 03:30 에 그렇게 나왔다(sca-y36)."""

    def _meta(self) -> SlowRequestMeta:
        return SlowRequestMeta(
            elapsed_wall=800.0, mono_elapsed=790.0, started=0.0, model="gemini-x",
            model_actual=None, effort="medium", num_turns=1, reason=None,
            session_id="세션1", resume=False, channel="C1", channel_name="테스트채널", text="원문",
        )

    def _detail(self, *, reports_output_tokens: bool = True, splits_tool_time: bool = True) -> str:
        formatter = SlowReportFormatter(assumed_tokens_per_sec=40)
        breakdown = TimeBreakdown(
            start_ts=0.0, end_ts=100.0, tool_sec=0.0, think_sec=0.0, wait_sec=100.0,
            reports_output_tokens=reports_output_tokens, splits_tool_time=splits_tool_time,
        )
        diagnosis = ElapsedDiagnostician(sleep_gap_suspect_sec=30).diagnose(800.0, 790.0)
        return formatter.format(self._meta(), diagnosis, breakdown)[1]

    def test_토큰을_안_남기면_사고와_대기를_안_나눈다(self) -> None:
        detail = self._detail(reports_output_tokens=False, splits_tool_time=False)

        assert "| 출력 토큰으로 설명되는 시간 |" not in detail
        assert "| 출력 토큰으로 설명 안 되는 시간 |" not in detail
        assert "| 엔진 처리(미분리) |" in detail

    def test_토큰을_안_남기면_그_사실을_적는다(self) -> None:
        detail = self._detail(reports_output_tokens=False, splits_tool_time=False)

        assert "출력 토큰을 남기지 않아" in detail

    def test_토큰을_안_남기면_단순_대기라고_결론짓지_않는다(self) -> None:
        """대기 100퍼센트는 계산 결과일 뿐 관측이 아니다."""
        detail = self._detail(reports_output_tokens=False, splits_tool_time=False)

        assert "출력 토큰으로 설명되지 않는 구간" not in detail

    def test_토큰을_안_남기면_긴_구간에_토큰_열을_안_낸다(self) -> None:
        """늘 "-" 인 열은 읽는 사람을 헷갈리게만 한다."""
        formatter = SlowReportFormatter(assumed_tokens_per_sec=40)
        from slack_cli_agent.observability.slow_report import GapDetail

        breakdown = TimeBreakdown(
            start_ts=0.0, end_ts=100.0, tool_sec=0.0, think_sec=0.0, wait_sec=100.0,
            top_gaps=(
                GapDetail(duration_sec=12.0, start_ts=0.0, end_ts=12.0, last_tool_brief="view_file",
                          output_tokens=None, think_sec=0.0, wait_sec=12.0),
            ),
            reports_output_tokens=False, splits_tool_time=False,
        )
        diagnosis = ElapsedDiagnostician(sleep_gap_suspect_sec=30).diagnose(800.0, 790.0)
        _, detail = formatter.format(self._meta(), diagnosis, breakdown)

        assert "구간 끝 출력 토큰" not in detail
        assert "사고/대기 근사" not in detail
        # 머리글만 줄이면 행이 길어져 표가 어긋난다. 둘을 함께 본다.
        assert "| 순위 | 소요 | 직전 도구 | 판정 |" in detail
        assert "| 1 | 12.0초 | view_file | - |" in detail

    def test_토큰을_남기면_긴_구간에_토큰_열이_그대로_있다(self) -> None:
        formatter = SlowReportFormatter(assumed_tokens_per_sec=40)
        from slack_cli_agent.observability.slow_report import GapDetail

        breakdown = TimeBreakdown(
            start_ts=0.0, end_ts=100.0, tool_sec=0.0, think_sec=80.0, wait_sec=20.0,
            top_gaps=(
                GapDetail(duration_sec=12.0, start_ts=0.0, end_ts=12.0, last_tool_brief="Bash ls",
                          output_tokens=480, think_sec=12.0, wait_sec=0.0),
            ),
        )
        diagnosis = ElapsedDiagnostician(sleep_gap_suspect_sec=30).diagnose(800.0, 790.0)
        _, detail = formatter.format(self._meta(), diagnosis, breakdown)

        assert "| 순위 | 소요 | 직전 도구 | 구간 끝 출력 토큰 | 사고/대기 근사 | 판정 |" in detail
        assert "| 1 | 12.0초 | Bash ls | 480 | 사고 12.0초 / 대기 0.0초 | - |" in detail

    def test_토큰을_남기면_전과_같다(self) -> None:
        detail = self._detail()

        assert "| 출력 토큰으로 설명되는 시간 |" in detail
        assert "출력 토큰으로 설명되지 않는 구간" in detail
        assert "출력 토큰을 남기지 않아" not in detail


class Test능력이_다른_조합(Test출력_토큰을_안_남기는_기록):
    """두 능력은 독립이다. 둘 다 False 인 조합만 보면 한쪽만 False 인 경우가
    안 걸린다(코덱스 지적)."""

    def test_토큰만_없고_도구는_가르면_도구_줄이_남는다(self) -> None:
        detail = self._detail(reports_output_tokens=False, splits_tool_time=True)

        assert "| 도구 실행 |" in detail
        assert "| 엔진 처리(미분리) |" in detail

    def test_비중이_낮으면_대부분이라고_안_한다(self) -> None:
        """미분리 구간이 30퍼센트인데 대부분이라고 적으면 거짓이다."""
        formatter = SlowReportFormatter(assumed_tokens_per_sec=40)
        breakdown = TimeBreakdown(
            start_ts=0.0, end_ts=100.0, tool_sec=70.0, think_sec=0.0, wait_sec=30.0,
            reports_output_tokens=False, splits_tool_time=True,
        )
        diagnosis = ElapsedDiagnostician(sleep_gap_suspect_sec=30).diagnose(800.0, 790.0)
        _, detail = formatter.format(self._meta(), diagnosis, breakdown)

        assert "시간 대부분이 엔진 처리" not in detail
        assert "도구 실행 자체가" in detail

    def test_토큰이_없으면_근사_안내문을_안_낸다(self) -> None:
        """나누지 않았다고 해 놓고 나눈 근사치라고 안내하면 어긋난다."""
        detail = self._detail(reports_output_tokens=False, splits_tool_time=False)

        assert "나눠 가른 근사치" not in detail

    def test_토큰이_있으면_근사_안내문을_낸다(self) -> None:
        assert "나눠 가른 근사치" in self._detail()

    def test_토큰이_없으면_긴_구간_설명이_중립이다(self) -> None:
        """제미나이에서는 그 구간이 대기라고 단정할 수 없다."""
        formatter = SlowReportFormatter(assumed_tokens_per_sec=40)
        from slack_cli_agent.observability.slow_report import GapDetail

        breakdown = TimeBreakdown(
            start_ts=0.0, end_ts=100.0, tool_sec=0.0, think_sec=0.0, wait_sec=100.0,
            top_gaps=(
                GapDetail(duration_sec=12.0, start_ts=0.0, end_ts=12.0, last_tool_brief="view_file",
                          output_tokens=None, think_sec=0.0, wait_sec=12.0),
            ),
            reports_output_tokens=False, splits_tool_time=False,
        )
        diagnosis = ElapsedDiagnostician(sleep_gap_suspect_sec=30).diagnose(800.0, 790.0)
        _, detail = formatter.format(self._meta(), diagnosis, breakdown)

        assert "다음 행동을 정하기까지의 대기입니다" not in detail
        assert "기록에 남은 단계 사이의 긴 구간입니다" in detail


class Test단정하지_않는다(Test출력_토큰을_안_남기는_기록):
    """사고 시간은 출력 토큰 수를 처리 속도로 나눈 근사이지 측정이 아니다.
    보고 문구가 근사보다 강하게 말하면 읽는 쪽이 실측으로 받는다(코덱스 지적)."""

    def _cause(self, **fields: float) -> str:
        formatter = SlowReportFormatter(assumed_tokens_per_sec=40)
        base = {"start_ts": 0.0, "end_ts": 100.0, "tool_sec": 0.0, "think_sec": 0.0, "wait_sec": 0.0}
        base.update(fields)
        breakdown = TimeBreakdown(**base)  # type: ignore[arg-type]
        diagnosis = ElapsedDiagnostician(sleep_gap_suspect_sec=30).diagnose(800.0, 790.0)
        return formatter.format(self._meta(), diagnosis, breakdown)[1]

    def test_재시도가_절반_이하면_대부분이라고_안_한다(self) -> None:
        detail = self._cause(retry_sec=40.0, wait_sec=60.0)

        assert "시간 대부분이 끊겼다" not in detail
        assert "끊겼다 다시 부르느라 버린 시간이 40.0초" in detail

    def test_재시도_구간이_요청과_무관하다고_단정하지_않는다(self) -> None:
        """캐시 사용량 역산 판정이라 effort 무관까지는 이 기록으로 못 말한다."""
        detail = self._cause(retry_sec=60.0, wait_sec=40.0)

        assert "effort 와 무관한 구간입니다" not in detail

    def test_긴_구간_설명이_직전_도구를_단정하지_않는다(self) -> None:
        """도구 없이 사용자 입력 뒤 모델이 답한 구간도 여기 들어온다."""
        from slack_cli_agent.observability.slow_report import GapDetail

        formatter = SlowReportFormatter(assumed_tokens_per_sec=40)
        breakdown = TimeBreakdown(
            start_ts=0.0, end_ts=100.0, tool_sec=0.0, think_sec=20.0, wait_sec=80.0,
            top_gaps=(
                GapDetail(duration_sec=12.0, start_ts=0.0, end_ts=12.0, last_tool_brief="Read",
                          output_tokens=100, think_sec=2.5, wait_sec=9.5),
            ),
        )
        diagnosis = ElapsedDiagnostician(sleep_gap_suspect_sec=30).diagnose(800.0, 790.0)
        _, detail = formatter.format(self._meta(), diagnosis, breakdown)

        assert "직전 도구를 처리한 뒤 다음 행동을 정하기까지의 대기입니다" not in detail
        assert "기록에 남은 단계 사이의 긴 구간입니다" in detail

    def test_토큰_미기록에_한쪽도_안_넘으면_세분화_불가로_끝낸다(self) -> None:
        """미분리 40퍼센트, 도구 25퍼센트면 어느 쪽도 대부분이 아니다."""
        formatter = SlowReportFormatter(assumed_tokens_per_sec=40)
        breakdown = TimeBreakdown(
            start_ts=0.0, end_ts=100.0, tool_sec=25.0, think_sec=0.0, wait_sec=40.0,
            reports_output_tokens=False, splits_tool_time=True,
        )
        diagnosis = ElapsedDiagnostician(sleep_gap_suspect_sec=30).diagnose(800.0, 790.0)
        _, detail = formatter.format(self._meta(), diagnosis, breakdown)

        assert "세분화할 수 없습니다" in detail
        assert "시간 대부분" not in detail


class Test비중이_백을_안_넘는다:
    """구간 합이 총 구간을 넘으면 비중이 100퍼센트를 넘어 표가 신뢰를 잃는다.
    실측 2026-09-19 04:17 에 101퍼센트로 나왔다(sca-be2)."""

    def test_구간_합이_더_크면_그것을_총_구간으로_쓴다(self) -> None:
        breakdown = TimeBreakdown(
            start_ts=0.0, end_ts=100.0, tool_sec=60.0, think_sec=0.0, wait_sec=45.0,
        )

        assert breakdown.total_span == 105.0

    def test_총_구간이_더_크면_그대로_쓴다(self) -> None:
        breakdown = TimeBreakdown(
            start_ts=0.0, end_ts=100.0, tool_sec=10.0, think_sec=0.0, wait_sec=20.0,
        )

        assert breakdown.total_span == 100.0


class Test재시도를_판정할_수_없는_기록:
    """재시도 판정은 요청별 cache_read/cache_creation 역산이다. 사용량 자체를
    안 남기는 기록에서는 영원히 "-" 라, 그 열이 0 과 미측정을 헷갈리게 한다
    (sca-ron). 출력 토큰 유무와는 독립 축이다(코덱스 판단)."""

    def _meta(self) -> SlowRequestMeta:
        return SlowRequestMeta(
            elapsed_wall=800.0, mono_elapsed=790.0, started=0.0, model="gemini-x",
            model_actual=None, effort="medium", num_turns=1, reason=None,
            session_id="세션1", resume=False, channel="C1", channel_name="테스트채널", text="원문",
        )

    def _detail(self, *, reports_cache_usage: bool) -> str:
        from slack_cli_agent.observability.slow_report import GapDetail

        formatter = SlowReportFormatter(assumed_tokens_per_sec=40)
        breakdown = TimeBreakdown(
            start_ts=0.0, end_ts=100.0, tool_sec=0.0, think_sec=0.0, wait_sec=100.0,
            top_gaps=(
                GapDetail(duration_sec=12.0, start_ts=0.0, end_ts=12.0, last_tool_brief="grep_search",
                          output_tokens=None, think_sec=0.0, wait_sec=12.0),
            ),
            reports_output_tokens=False, splits_tool_time=False,
            reports_cache_usage=reports_cache_usage,
        )
        diagnosis = ElapsedDiagnostician(sleep_gap_suspect_sec=30).diagnose(800.0, 790.0)
        return formatter.format(self._meta(), diagnosis, breakdown)[1]

    def test_사용량이_없으면_판정_열을_뺀다(self) -> None:
        detail = self._detail(reports_cache_usage=False)

        assert "| 판정 |" not in detail
        assert "| 직전 도구 |" in detail

    def test_사용량이_있으면_판정_열을_낸다(self) -> None:
        """출력 토큰이 없어도 캐시 사용량은 있을 수 있다. 두 축은 독립이다."""
        detail = self._detail(reports_cache_usage=True)

        assert "| 판정 |" in detail

    def test_계산기가_사용량_능력을_그대로_전한다(self) -> None:
        class 사용량없는리더(SessionTranscriptReader):
            @property
            def reports_cache_usage(self) -> bool:
                return False

            def read(self, session_id: str) -> list[TranscriptEvent]:
                return [
                    TranscriptEvent(ts=0.0, role="user", kind="text", brief="", output_tokens=None),
                    TranscriptEvent(ts=10.0, role="assistant", kind="text", brief="", output_tokens=None),
                ]

        breakdown = TimeBreakdownCalculator(assumed_tokens_per_sec=40).compute(사용량없는리더(), "S1")

        assert breakdown is not None
        assert breakdown.reports_cache_usage is False


class Test계산기가_리더의_능력을_그대로_전한다:
    def test_못_가르는_리더면_표시가_따라온다(self) -> None:
        class 못가르는리더(SessionTranscriptReader):
            @property
            def splits_tool_time(self) -> bool:
                return False

            @property
            def reports_output_tokens(self) -> bool:
                return False

            def read(self, session_id: str) -> list[TranscriptEvent]:
                return [
                    TranscriptEvent(ts=0.0, role="user", kind="text", brief="", output_tokens=None),
                    TranscriptEvent(ts=10.0, role="assistant", kind="text", brief="", output_tokens=None),
                ]

        breakdown = TimeBreakdownCalculator(assumed_tokens_per_sec=40).compute(못가르는리더(), "S1")

        assert breakdown is not None
        assert breakdown.splits_tool_time is False
        assert breakdown.reports_output_tokens is False
        assert breakdown.reports_cache_usage is True  # 이 대역은 사용량을 남긴다


class Test엔진원문은_보고에_담기지_않는다:
    """이 보고는 요청이 온 채널이 아니라 트러블슈팅 채널로 간다. stdout 은
    엔진 응답 본문이라 원 대화·읽은 파일·링크된 스레드를 인용할 수 있다.
    그래서 원문을 담을 자리를 타입에서 없앴다(sca-r25). 필터가 아니라 타입으로
    막아야 나중에 추가되는 필드가 기본값으로 새지 않는다.
    """

    #: 이 보고가 실어도 되는 것 전부. 금지 목록이 아니라 허용 목록인 이유는
    #: engine_output 이나 result_text 같은 새 이름이 금지 목록을 그냥 지나가기
    #: 때문이다. 여기에 이름을 더하려면 그 값이 다른 채널에 나가도 되는지를
    #: 먼저 판단하게 된다.
    허용_필드: ClassVar[frozenset[str]] = frozenset({
        "elapsed_wall", "mono_elapsed", "started", "model", "model_actual", "effort",
        "num_turns", "reason", "session_id", "resume", "channel", "channel_name",
        "text", "usage", "engine",
    })

    def test_보고_타입에_허가되지_않은_필드가_없다(self) -> None:
        assert {f.name for f in fields(SlowRequestMeta)} == self.허용_필드

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

    def test_판정_불가_항목은_0으로_안_적는다(self) -> None:
        """agy 는 캐시 기록을 아예 안 낸다. 0 으로 적으면 잰 값이 0인 것과
        구분되지 않고, 합계에 넣으면 합계도 같은 거짓말을 한다."""
        builder = UsageRowBuilder(owner_only_channels=frozenset({"TS"}))
        usage = Usage(
            input_tokens=100, output_tokens=50,
            unavailable=frozenset({"cache_creation_tokens", "cache_read_tokens"}),
        )
        detail = builder.build(usage, troubleshoot_channel="TS")[0][1]
        assert "캐시 기록 판정 불가" in detail
        assert "캐시 읽기 판정 불가" in detail
        assert "캐시 기록 0" not in detail

    def test_판정_불가가_있으면_합계를_확정으로_안_적는다(self) -> None:
        builder = UsageRowBuilder(owner_only_channels=frozenset({"TS"}))
        usage = Usage(
            input_tokens=100, output_tokens=50, unavailable=frozenset({"cache_read_tokens"}),
        )
        detail = builder.build(usage, troubleshoot_channel="TS")[0][1]
        assert detail.startswith("150개 이상")

    def test_전부_잰_값이면_합계를_그대로_적는다(self) -> None:
        builder = UsageRowBuilder(owner_only_channels=frozenset({"TS"}))
        usage = Usage(input_tokens=100, output_tokens=50, cache_creation_tokens=10, cache_read_tokens=20)
        detail = builder.build(usage, troubleshoot_channel="TS")[0][1]
        assert detail.startswith("180개 (")

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
        calculator = SessionContextCalculator(context_limit={})
        assert calculator.compute(reader, "s1").used == 100

    def test_assistant가_아닌_턴은_세지_않는다(self) -> None:
        reader = FakeTranscriptReader([
            self._event(1.0, "assistant", input=1, output=2, creation=3, read=4),
            self._event(2.0, "user", input=999),
        ])
        calculator = SessionContextCalculator(context_limit={})
        assert calculator.compute(reader, "s1").used == 10

    def test_usage가_없는_assistant_턴은_건너뛴다(self) -> None:
        reader = FakeTranscriptReader([
            self._event(1.0, "assistant", input=1, output=2, creation=3, read=4),
            self._event(2.0, "assistant"),
        ])
        calculator = SessionContextCalculator(context_limit={})
        assert calculator.compute(reader, "s1").used == 10

    def test_기록이_없으면_사용량이_None이다(self) -> None:
        reader = FakeTranscriptReader([])
        calculator = SessionContextCalculator(context_limit={})
        assert calculator.compute(reader, "s1").used is None

    def test_표에_있는_모델이면_한도를_함께_돌려준다(self) -> None:
        reader = FakeTranscriptReader([])
        calculator = SessionContextCalculator(context_limit={"m": 200000})
        assert calculator.compute(reader, "s1", model="m").limit == 200000

    def test_표에_없는_모델이면_한도가_None이다(self) -> None:
        reader = FakeTranscriptReader([])
        calculator = SessionContextCalculator(context_limit={"m": 200000})
        assert calculator.compute(reader, "s1", model="다른모델").limit is None


class TestUsageRowBuilder세션행:
    """원본 `usage_rows()` 의 "세션" 행 이관 검증.

    토큰 행과 같은 채널 조건 아래 있다. 계산기를 안 주면 세션 행 자체를
    내지 않는다 — 세션 기록 경로가 없는 조립 코드에서 잘못된 값을 내는 것보다
    행이 없는 편이 낫다.
    """

    def _builder(self, used: int | None, limit: int | None) -> UsageRowBuilder:
        class 고정계산기:
            def compute(
                self, reader: SessionTranscriptReader, session_id: str, model: str | None = None,
            ) -> SessionContext:
                return SessionContext(used=used, limit=limit)

        return UsageRowBuilder(owner_only_channels=frozenset({"TS"}), session_context=고정계산기())

    def test_한도를_알면_비율을_적는다(self) -> None:
        rows = self._builder(100_000, 200_000).build(
            None, "TS", reader=FakeTranscriptReader([]), session_id="s1", model="m")
        assert rows[1][0] == "세션"
        assert "100k/200k" in rows[1][1]
        assert "50퍼센트" in rows[1][1]

    def test_한도를_모르면_비율을_안_적는다(self) -> None:
        rows = self._builder(100_000, None).build(
            None, "TS", reader=FakeTranscriptReader([]), session_id="s1", model="m")
        assert "퍼센트" not in rows[1][1]
        assert "한도 미상" in rows[1][1]

    def test_사용량을_못_읽으면_사유를_적는다(self) -> None:
        rows = self._builder(None, 200_000).build(
            None, "TS", reader=FakeTranscriptReader([]), session_id="s1", model="m")
        assert "조회 실패" in rows[1][1]

    def test_소유자_전용_채널이_아니면_세션_행도_안_낸다(self) -> None:
        rows = self._builder(100_000, 200_000).build(None, "다른채널", session_id="s1", model="m")
        assert rows == []

    def test_계산기가_없으면_세션_행이_없다(self) -> None:
        builder = UsageRowBuilder(owner_only_channels=frozenset({"TS"}))
        rows = builder.build(None, "TS", reader=FakeTranscriptReader([]), session_id="s1", model="m")
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
