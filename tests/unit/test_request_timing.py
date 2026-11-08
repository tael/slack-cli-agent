"""요청별 시간 분해 시험 — observability/request_timing.py (S0 계측).

기존 느린 요청 분해는 세션 기록 첫 줄과 끝 줄 사이만 나눠, 959초 요청의
21.6퍼센트만 설명하고 대기를 0.2초로 적었다. 여기서는 새 분해가 요청의
벽시계 시간을 빠짐없이 나눠 항목 합이 elapsed 와 같은지, 서브에이전트·MCP·
훅·마무리 검수 재작성·기동이 각각 제 항목에 들어가는지를 본다.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_pipeline import build_pipeline, make_ctx, ok_response

from slack_cli_agent.engine.transcript import (
    ClaudeTranscriptReader,
    SessionTranscriptReader,
    TimingEvent,
    TranscriptEvent,
)
from slack_cli_agent.observability.request_timing import (
    PART_KEYS,
    RequestTimingCalculator,
    attribute,
    tool_category,
)


def _ev(ts: float, kind: str, **kw: Any) -> TimingEvent:
    return TimingEvent(ts=ts, kind=kind, **kw)


class Test_도구_분류:
    def test_서브에이전트와_MCP와_일반_도구를_가른다(self) -> None:
        assert tool_category("Task") == "subagent_sec"
        assert tool_category("Agent") == "subagent_sec"
        assert tool_category("mcp__slack__read") == "mcp_sec"
        assert tool_category("Bash") == "tool_sec"


class Test_시간_나누기:
    def _full_request(self) -> list[TimingEvent]:
        # 100 시작, 엔진 110 기동, 첫 모델 요청 130.
        return [
            _ev(105, "assistant"),  # 이전 요청의 줄, 창 밖이라 버린다
            _ev(120, "other"),  # 큐 등록, 기동 중
            _ev(130, "api_request"),
            _ev(140, "tool_use", tool_uses=(("a", "Bash"), ("b", "Bash"))),
            _ev(150, "tool_result", tool_results=("a",)),
            # 두 결과 사이도 도구 시간이다. 옛 분해는 이 구간을 버렸다.
            _ev(155, "tool_result", tool_results=("b",)),
            _ev(155, "hook", hook_sec=2.0),  # PostToolUse 훅, 결과 뒤에 적힌다
            _ev(155, "api_request"),
            _ev(165, "tool_use", tool_uses=(("c", "Agent"),)),
            _ev(225, "tool_result", tool_results=("c",)),
            _ev(225, "api_request"),
            _ev(230, "tool_use", tool_uses=(("d", "mcp__db__query"),)),
            _ev(250, "tool_result", tool_results=("d",)),
            _ev(250, "api_request"),
            _ev(260, "assistant"),
            _ev(261, "hook", hook_sec=1.0),  # Stop 훅 요약
            _ev(261, "stop_block"),
            _ev(261, "api_request"),
            _ev(281, "assistant"),
        ]

    def test_항목_합이_elapsed_와_같다(self) -> None:
        timing = attribute(
            self._full_request(), start=100, engine_start=110, engine_end=290, end=292,
            queue_wait_sec=4.0, mono_elapsed=190,
        )
        assert timing.elapsed_sec == 192
        assert timing.parts_sum == pytest.approx(192)
        assert timing.pre_engine_sec == pytest.approx(10)
        assert timing.startup_sec == pytest.approx(20)
        # 130-140 + 155-165 + 225-230 + 250-260 + 260-261 중 훅 1초를 뺀 값
        assert timing.model_api_sec == pytest.approx(10 + 10 + 5 + 10 + 0)
        assert timing.tool_sec == pytest.approx(15 - 2)
        assert timing.subagent_sec == pytest.approx(60)
        assert timing.mcp_sec == pytest.approx(20)
        assert timing.hook_sec == pytest.approx(3)
        assert timing.finish_review_sec == pytest.approx(20)
        assert timing.finish_review_blocks == 1
        assert timing.other_sec == pytest.approx(9 + 2)
        assert timing.unattributed_sec == 0
        assert timing.queue_wait_sec == 4.0
        assert timing.wall_minus_mono_sec == pytest.approx(2)
        assert timing.transcript is True

    def test_병렬_호출은_서브에이전트가_먼저다(self) -> None:
        events = [
            _ev(10, "api_request"),
            _ev(11, "tool_use", tool_uses=(("a", "Bash"), ("b", "Task"), ("c", "mcp__x__y"))),
            _ev(21, "tool_result", tool_results=("a", "b", "c")),
        ]
        timing = attribute(events, start=10, engine_start=10, engine_end=21)
        assert timing.subagent_sec == pytest.approx(10)
        assert timing.tool_sec == 0
        assert timing.mcp_sec == 0

    def test_id_없는_결과는_가장_오래된_호출을_닫는다(self) -> None:
        events = [
            _ev(0, "tool_use", tool_uses=(("#1", "Bash"),)),
            _ev(5, "tool_result"),
            _ev(8, "assistant"),
        ]
        timing = attribute(events, start=0, engine_start=0, engine_end=8)
        assert timing.tool_sec == pytest.approx(5)
        assert timing.model_api_sec == pytest.approx(3)

    def test_세션_기록이_없으면_엔진_시간은_미분류로_남긴다(self) -> None:
        timing = attribute([], start=0, engine_start=3, engine_end=50, queue_wait_sec=None)
        assert timing.pre_engine_sec == pytest.approx(3)
        assert timing.unattributed_sec == pytest.approx(47)
        assert timing.parts_sum == pytest.approx(50)
        assert timing.transcript is False

    def test_audit_사전은_모든_항목과_합을_싣는다(self) -> None:
        record = attribute([], start=0, engine_start=0, engine_end=1, queue_wait_sec=2.5).as_audit_dict()
        for key in PART_KEYS:
            assert key in record
        assert record["queue_wait_sec"] == 2.5
        assert record["parts_sum_sec"] == record["elapsed_sec"] == 1
        json.dumps(record)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def _write_claude_transcript(home: Path, workdir: Path, session_id: str, lines: list[dict[str, Any]]) -> None:
    reader = ClaudeTranscriptReader(workdir, home=home)
    path = reader.transcript_path(session_id)
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


class Test_클로드_기록_읽기:
    def test_타이밍에_필요한_줄을_모두_읽는다(self, tmp_path: Path) -> None:
        workdir = tmp_path / "work"
        t0 = 1_800_000_000.0
        lines = [
            {"type": "assistant", "timestamp": _iso(t0 - 500), "message": {"content": [{"type": "text"}]}},
            {"type": "queue-operation", "operation": "enqueue", "timestamp": _iso(t0)},
            {"type": "api-request", "timestamp": _iso(t0 + 1)},
            {"type": "assistant", "timestamp": _iso(t0 + 2), "isSidechain": False, "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Agent", "input": {}},
            ]}},
            # 서브에이전트 자신의 줄은 부모의 Agent 호출이 이미 덮는다.
            {"type": "assistant", "timestamp": _iso(t0 + 3), "isSidechain": True, "message": {"content": [
                {"type": "tool_use", "id": "s1", "name": "Bash", "input": {}},
            ]}},
            {"type": "user", "timestamp": _iso(t0 + 9), "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1"},
            ]}},
            {"type": "attachment", "timestamp": _iso(t0 + 9), "attachment": {
                "type": "hook_success", "hookEvent": "PostToolUse", "durationMs": 40,
            }},
            {"type": "attachment", "timestamp": _iso(t0 + 10), "attachment": {
                "type": "hook_blocking_error", "hookEvent": "Stop",
            }},
            {"type": "system", "subtype": "stop_hook_summary", "timestamp": _iso(t0 + 10),
             "hookInfos": [{"durationMs": 96}, {"durationMs": 4}]},
        ]
        _write_claude_transcript(tmp_path, workdir, "sess", lines)
        events = ClaudeTranscriptReader(workdir, home=tmp_path).read_timing("sess", since_ts=t0)
        assert [event.kind for event in events] == [
            "other", "api_request", "tool_use", "tool_result", "hook", "stop_block", "hook",
        ]
        assert events[2].tool_uses == (("t1", "Agent"),)
        assert events[3].tool_results == ("t1",)
        assert events[4].hook_sec == pytest.approx(0.04)
        assert events[6].hook_sec == pytest.approx(0.1)

    def test_기록이_없으면_빈_목록(self, tmp_path: Path) -> None:
        assert ClaudeTranscriptReader(tmp_path, home=tmp_path).read_timing("none") == []


class _OnlyReadReader(SessionTranscriptReader):
    def read(self, session_id: str) -> list[TranscriptEvent]:
        return [
            TranscriptEvent(ts=1, role="user", kind="text", brief="", output_tokens=None),
            TranscriptEvent(ts=2, role="assistant", kind="tool_use", brief="shell ls", output_tokens=None),
            TranscriptEvent(ts=5, role="user", kind="tool_result", brief="", output_tokens=None),
            TranscriptEvent(ts=7, role="assistant", kind="text", brief="", output_tokens=None),
        ]


class Test_기본_읽기:
    def test_read_만_있는_엔진도_도구와_모델을_가른다(self) -> None:
        events = _OnlyReadReader().read_timing("s")
        timing = attribute(events, start=0, engine_start=0, engine_end=7)
        assert timing.startup_sec == pytest.approx(1)
        assert timing.tool_sec == pytest.approx(3)
        assert timing.model_api_sec == pytest.approx(3)


class _ExplodingReader(_OnlyReadReader):
    def read_timing(self, session_id: str, since_ts: float | None = None) -> list[TimingEvent]:
        raise OSError("boom")


class Test_계산기:
    def test_읽기가_터져도_행은_남는다(self) -> None:
        calc = RequestTimingCalculator(lambda engine: _ExplodingReader())
        timing = calc.compute(engine="claude", session_id="s", start=0, engine_start=1, engine_end=9)
        assert timing.unattributed_sec == pytest.approx(8)

    def test_세션_ID_가_없으면_읽지_않는다(self) -> None:
        def resolver(engine: str) -> SessionTranscriptReader:
            raise AssertionError("읽으면 안 된다")

        timing = RequestTimingCalculator(resolver).compute(
            engine="", session_id="", start=0, engine_start=0, engine_end=2,
        )
        assert timing.unattributed_sec == pytest.approx(2)


class _FixedReader(SessionTranscriptReader):
    def __init__(self) -> None:
        self.asked: list[tuple[str, float | None]] = []

    def read(self, session_id: str) -> list[TranscriptEvent]:
        return []

    def read_timing(self, session_id: str, since_ts: float | None = None) -> list[TimingEvent]:
        self.asked.append((session_id, since_ts))
        assert since_ts is not None
        return [
            TimingEvent(ts=since_ts + 1, kind="api_request"),
            TimingEvent(ts=since_ts + 2, kind="tool_use", tool_uses=(("x", "Task"),)),
            TimingEvent(ts=since_ts + 6, kind="tool_result", tool_results=("x",)),
        ]


class Test_파이프라인_기록:
    def _clock(self, start: float):
        state = {"t": start}

        def now() -> float:
            state["t"] += 10.0
            return state["t"]

        return now

    def test_요청_기록에_시간_분해가_실린다(self, tmp_path: Path) -> None:
        reader = _FixedReader()
        pipeline, deps = build_pipeline(
            responses=[ok_response(session_id="sess-1")], tmp_path=tmp_path,
            now=self._clock(1_700_000_000.0),
            request_timing=RequestTimingCalculator(lambda engine: reader),
        )
        pipeline.handle(make_ctx())
        record = deps["audit"].records[-1]
        breakdown = record["time_breakdown"]
        assert breakdown["parts_sum_sec"] == pytest.approx(record["elapsed"], abs=0.05)
        assert breakdown["subagent_sec"] == pytest.approx(4)
        assert breakdown["transcript"] is True
        # 메시지 게시 시각에서 처리 시작까지. 재큐가 아니어도 남는다.
        assert breakdown["queue_wait_sec"] is not None
        assert breakdown["queue_wait_sec"] > 0
        # 예전 필드는 그대로다.
        assert "queue_wait_sec" in record and record["queue_wait_sec"] is None
        assert reader.asked and reader.asked[0][0] == "sess-1"

    def test_계산기가_없으면_필드를_넣지_않는다(self, tmp_path: Path) -> None:
        pipeline, deps = build_pipeline(responses=[ok_response()], tmp_path=tmp_path)
        pipeline.handle(make_ctx())
        assert "time_breakdown" not in deps["audit"].records[-1]
