"""engine/transcript.py 의 세션 기록 파서 테스트.

원본 bot.py 의 `claude_transcript()`/`time_breakdown()` 앞부분(이벤트 파싱)을
분리한 `ClaudeTranscriptReader` 를 검증한다. jsonl 형식 의존은 이 클래스
안에 갇혀 있어야 하고, 호출부는 `TranscriptEvent` 목록만 본다.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from slack_cli_agent.config.profile import EngineSpec
from slack_cli_agent.engine.transcript import (
    ClaudeTranscriptReader,
    CodexTranscriptReader,
    SessionTranscriptReader,
    TranscriptEvent,
    TranscriptReaderRegistry,
)


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines),
        encoding="utf-8",
    )


class TestClaudeTranscriptReader경로:
    def test_workdir_슬러그로_경로를_만든다(self, tmp_path: Path) -> None:
        reader = ClaudeTranscriptReader(workdir=Path("/Users/x/proj.name"), home=tmp_path)
        path = reader.transcript_path("세션1")
        assert path == tmp_path / ".claude" / "projects" / "-Users-x-proj-name" / "세션1.jsonl"

    def test_파일이_없으면_빈_목록이다(self, tmp_path: Path) -> None:
        reader = ClaudeTranscriptReader(workdir=Path("/w"), home=tmp_path)
        assert reader.read("없는세션") == []


class TestClaudeTranscriptReader읽기:
    def test_tool_use와_text를_이벤트로_읽는다(self, tmp_path: Path) -> None:
        reader = ClaudeTranscriptReader(workdir=Path("/w"), home=tmp_path)
        path = reader.transcript_path("s1")
        _write_jsonl(path, [
            {
                "timestamp": "2026-09-01T00:00:00Z",
                "type": "assistant",
                "message": {
                    "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}}],
                    "usage": {"output_tokens": 10},
                },
            },
            {
                "timestamp": "2026-09-01T00:00:05Z",
                "type": "user",
                "message": {"content": [{"type": "tool_result"}]},
            },
        ])
        events = reader.read("s1")
        ts0 = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC).timestamp()
        ts1 = datetime(2026, 9, 1, 0, 0, 5, tzinfo=UTC).timestamp()
        assert events == [
            TranscriptEvent(ts=ts0, role="assistant", kind="tool_use",
                             brief="Bash ls -la", output_tokens=10),
            TranscriptEvent(ts=ts1, role="user", kind="tool_result",
                             brief="", output_tokens=None),
        ]

    def test_시각을_못읽는_줄은_건너뛴다(self, tmp_path: Path) -> None:
        reader = ClaudeTranscriptReader(workdir=Path("/w"), home=tmp_path)
        path = reader.transcript_path("s2")
        _write_jsonl(path, [
            {"timestamp": None, "type": "assistant", "message": {"content": []}},
            {"timestamp": "2026-09-01T00:00:00Z", "type": "assistant", "message": {"content": []}},
        ])
        events = reader.read("s2")
        assert len(events) == 1

    def test_깨진_json_줄은_건너뛴다(self, tmp_path: Path) -> None:
        reader = ClaudeTranscriptReader(workdir=Path("/w"), home=tmp_path)
        path = reader.transcript_path("s3")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "{ 깨진 줄\n"
            + json.dumps({"timestamp": "2026-09-01T00:00:00Z", "type": "assistant",
                          "message": {"content": []}}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        events = reader.read("s3")
        assert len(events) == 1

    def test_시각순으로_정렬한다(self, tmp_path: Path) -> None:
        reader = ClaudeTranscriptReader(workdir=Path("/w"), home=tmp_path)
        path = reader.transcript_path("s4")
        _write_jsonl(path, [
            {"timestamp": "2026-09-01T00:00:10Z", "type": "assistant", "message": {"content": []}},
            {"timestamp": "2026-09-01T00:00:00Z", "type": "assistant", "message": {"content": []}},
        ])
        events = reader.read("s4")
        assert [e.ts for e in events] == sorted(e.ts for e in events)

    def test_캐시_사용량과_요청id를_읽는다(self, tmp_path: Path) -> None:
        """재시도 감지에 쓸 값이다. 원본 `time_breakdown()` 앞부분의 이벤트
        파싱(3260행 근처)이 usage 의 cache_creation_input_tokens·
        cache_read_input_tokens 와 줄의 requestId 를 함께 담는 것과 같다."""
        reader = ClaudeTranscriptReader(workdir=Path("/w"), home=tmp_path)
        path = reader.transcript_path("s5")
        _write_jsonl(path, [
            {
                "timestamp": "2026-09-01T00:00:00Z",
                "type": "assistant",
                "requestId": "req-1",
                "message": {
                    "content": [{"type": "text"}],
                    "usage": {
                        "output_tokens": 10,
                        "cache_creation_input_tokens": 100,
                        "cache_read_input_tokens": 50,
                    },
                },
            },
        ])
        events = reader.read("s5")
        assert len(events) == 1
        assert events[0].cache_creation_tokens == 100
        assert events[0].cache_read_tokens == 50
        assert events[0].request_id == "req-1"

    def test_사용량이나_요청id가_없으면_None이다(self, tmp_path: Path) -> None:
        reader = ClaudeTranscriptReader(workdir=Path("/w"), home=tmp_path)
        path = reader.transcript_path("s6")
        _write_jsonl(path, [
            {"timestamp": "2026-09-01T00:00:00Z", "type": "user", "message": {"content": []}},
        ])
        events = reader.read("s6")
        assert events[0].cache_creation_tokens is None
        assert events[0].cache_read_tokens is None
        assert events[0].request_id is None


class TestClaudeTranscriptReader입력토큰:
    """세션 컨텍스트 사용량 계산에 필요한 `input_tokens` 를 읽는지 본다.

    원본 `session_context()`(bot.py 3384행)는 마지막 assistant 턴의
    `input_tokens + cache_creation_input_tokens + cache_read_input_tokens +
    output_tokens` 를 그 세션이 지금 쓰는 컨텍스트 크기로 본다. 파서가
    `input_tokens` 를 안 담으면 그 합이 항상 실제보다 작게 나온다.
    """

    def test_usage의_input_tokens를_담는다(self, tmp_path: Path) -> None:
        reader = ClaudeTranscriptReader(workdir=Path("/w"), home=tmp_path)
        _write_jsonl(reader.transcript_path("s1"), [
            {
                "timestamp": "2026-09-01T00:00:00Z",
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "답"}],
                    "usage": {
                        "input_tokens": 11,
                        "output_tokens": 22,
                        "cache_creation_input_tokens": 33,
                        "cache_read_input_tokens": 44,
                    },
                },
            },
        ])
        event = reader.read("s1")[0]
        assert event.input_tokens == 11

    def test_usage가_없으면_input_tokens는_None이다(self, tmp_path: Path) -> None:
        reader = ClaudeTranscriptReader(workdir=Path("/w"), home=tmp_path)
        _write_jsonl(reader.transcript_path("s1"), [
            {
                "timestamp": "2026-09-01T00:00:00Z",
                "type": "user",
                "message": {"content": [{"type": "text", "text": "질문"}]},
            },
        ])
        assert reader.read("s1")[0].input_tokens is None


class 남의리더(SessionTranscriptReader):
    """밖에서 등록하는 리더의 현재 형태 — workdir, home, spec 3개를 받는다."""

    def __init__(self, workdir: Path, home: Path | None = None, spec: EngineSpec | None = None) -> None:
        self.workdir = workdir
        self.home = home
        self.spec = spec

    def read(self, session_id: str) -> list[TranscriptEvent]:
        return []


class Test기록리더선택:
    """엔진마다 세션 기록 형식이 다르다. 어느 리더를 쓸지 엔진 이름으로 고른다.

    원본은 `claude_transcript()`(bot.py:3076)에서 codex 프로필이면 바로 None 을
    돌려준다. 주석에 근거가 적혀 있다 — "없는 파일을 찾아 헤매다 빈 표를 내는
    것보다 낫다". 우리는 그 판정을 안 옮겨, codex 프로필에서도 Claude 기록
    경로를 뒤지고 있었다.
    """

    def test_claude_는_claude_리더를_준다(self, tmp_path: Path) -> None:
        reader = TranscriptReaderRegistry().create("claude", tmp_path)
        assert isinstance(reader, ClaudeTranscriptReader)

    def test_codex_는_claude_기록이_있어도_안_읽는다(self, tmp_path: Path) -> None:
        """codex 는 같은 형식의 기록을 안 남긴다. 그 경로를 찾지 않는다.

        "찾지 않는다" 는 결과가 빈 목록인 것만으로는 안 드러난다 — Claude
        리더도 파일이 없으면 빈 목록이다. 기록이 실제로 있는 상태를 만들어
        대조한다.
        """
        workdir = tmp_path / "work"
        home = tmp_path / "home"
        registry = TranscriptReaderRegistry()

        claude = registry.create("claude", workdir, home=home)
        assert isinstance(claude, ClaudeTranscriptReader)
        path = claude.transcript_path("S1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({
                "timestamp": "2026-09-14T10:00:00Z",
                "message": {"content": [{"type": "text", "text": "답"}]},
            }) + "\n",
            encoding="utf-8",
        )
        # 대조군 — 같은 자리를 Claude 리더는 읽는다
        assert claude.read("S1") != []

        assert registry.create("codex", workdir, home=home).read("S1") == []

    def test_모르는_엔진도_빈_기록을_준다(self, tmp_path: Path) -> None:
        """예외를 내지 않는다. 구간 분해가 실패해도 이미 끝난 요청 처리를 망기면 안 된다."""
        reader = TranscriptReaderRegistry().create("남의엔진", tmp_path)
        assert reader.read("어떤세션") == []

    def test_등록을_열어_둔다(self, tmp_path: Path) -> None:
        registry = TranscriptReaderRegistry()
        registry.register("남의엔진", 남의리더)
        assert isinstance(registry.create("남의엔진", tmp_path), 남의리더)

    def test_spec_을_받는_factory에_spec이_그대로_전달된다(self, tmp_path: Path) -> None:
        registry = TranscriptReaderRegistry()
        registry.register("남의엔진", 남의리더)
        spec = EngineSpec.from_dict({"type": "남의엔진", "binary": "x", "model": "m"})
        reader = registry.create("남의엔진", tmp_path, spec=spec)
        assert isinstance(reader, 남의리더)
        assert reader.spec is spec

    def test_spec_이전의_두_인자_factory도_그대로_동작한다(self) -> None:
        """세 번째 인자를 항상 넘기게 되면서, 밖에서 등록해 둔 옛 형태의
        factory 가 요청 시점에 TypeError 로 터진다. 등록할 때 감싸 둔다."""
        class 옛리더(SessionTranscriptReader):
            def __init__(self, workdir: Path, home: Path | None = None) -> None:
                self.workdir = workdir

            def read(self, session_id: str) -> list[TranscriptEvent]:
                return []

        registry = TranscriptReaderRegistry()
        registry.register("옛엔진", 옛리더)
        spec = EngineSpec.from_dict({"type": "옛엔진", "binary": "x", "model": "m"})
        assert isinstance(registry.create("옛엔진", Path("/w"), spec=spec), 옛리더)

    def test_spec_은_위치인자로_못_넘긴다(self) -> None:
        """위치로 받으면 home 자리와 헷갈려 조용히 빠진다. 빠지면 격리
        프로필에서 기본 홈을 보게 된다."""
        registry = TranscriptReaderRegistry()
        spec = EngineSpec.from_dict({"type": "claude", "binary": "x", "model": "m"})
        with pytest.raises(TypeError):
            registry.create("claude", Path("/w"), None, spec)  # type: ignore[misc]

    def test_등록소끼리_서로_영향이_없다(self, tmp_path: Path) -> None:
        first = TranscriptReaderRegistry()
        first.register("남의엔진", 남의리더)
        assert not isinstance(TranscriptReaderRegistry().create("남의엔진", tmp_path), 남의리더)


def _write_codex_rollout(home: Path, session_id: str, lines: list[dict]) -> Path:
    return _write_codex_rollout_at(home / ".codex" / "sessions", session_id, lines)


def _write_codex_rollout_at(sessions_root: Path, session_id: str, lines: list[dict]) -> Path:
    """Same fixture shape, but rooted directly at a sessions directory —
    for CODEX_HOME isolation, where the root isn't <home>/.codex/sessions."""
    path = sessions_root / "2026" / "09" / "16" / f"rollout-2026-09-16T00-47-42-{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines),
        encoding="utf-8",
    )
    return path


class TestCodexTranscriptReader경로:
    def test_세션id_접미사로_회전본을_찾는다(self, tmp_path: Path) -> None:
        reader = CodexTranscriptReader(workdir=Path("/w"), home=tmp_path)
        expected = _write_codex_rollout(tmp_path, "세션1", [
            {"timestamp": "2026-09-16T00:47:42Z", "type": "session_meta", "payload": {"session_id": "세션1"}},
        ])
        assert reader.find_transcript_path("세션1") == expected

    def test_회전본이_없으면_None이다(self, tmp_path: Path) -> None:
        reader = CodexTranscriptReader(workdir=Path("/w"), home=tmp_path)
        (tmp_path / ".codex" / "sessions").mkdir(parents=True)
        assert reader.find_transcript_path("없는세션") is None

    def test_codex_디렉터리_자체가_없으면_None이다(self, tmp_path: Path) -> None:
        reader = CodexTranscriptReader(workdir=Path("/w"), home=tmp_path)
        assert reader.find_transcript_path("아무거나") is None

    def test_read_파일이_없으면_빈_목록이다(self, tmp_path: Path) -> None:
        reader = CodexTranscriptReader(workdir=Path("/w"), home=tmp_path)
        assert reader.read("없는세션") == []


class TestCodexTranscriptReader읽기:
    """~/.codex/sessions 아래 실제 rollout 파일(codex_exec 이 originator 인 것,
    2026-09-16 실측)에서 뽑은 형태의 fixture 다. 민감한 프롬프트·추론 내용은
    빼고 계약에 필요한 필드만 남겼다."""

    def test_message를_text로_읽는다(self, tmp_path: Path) -> None:
        reader = CodexTranscriptReader(workdir=Path("/w"), home=tmp_path)
        _write_codex_rollout(tmp_path, "s1", [
            {
                "timestamp": "2026-09-15T15:47:42.998Z",
                "type": "response_item",
                "payload": {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "질문"}],
                },
            },
            {
                "timestamp": "2026-09-15T15:47:49.483Z",
                "type": "response_item",
                "payload": {
                    "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "답"}],
                },
            },
        ])
        events = reader.read("s1")
        assert [(e.role, e.kind, e.brief) for e in events] == [
            ("user", "text", ""),
            ("assistant", "text", ""),
        ]

    def test_reasoning을_thinking으로_assistant_귀속으로_읽는다(self, tmp_path: Path) -> None:
        reader = CodexTranscriptReader(workdir=Path("/w"), home=tmp_path)
        _write_codex_rollout(tmp_path, "s2", [
            {
                "timestamp": "2026-09-15T15:47:45.589Z",
                "type": "response_item",
                "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "계획"}]},
            },
        ])
        events = reader.read("s2")
        assert len(events) == 1
        assert events[0].role == "assistant"
        assert events[0].kind == "thinking"

    def test_custom_tool_call과_결과를_tool_use_tool_result로_읽는다(self, tmp_path: Path) -> None:
        reader = CodexTranscriptReader(workdir=Path("/w"), home=tmp_path)
        _write_codex_rollout(tmp_path, "s3", [
            {
                "timestamp": "2026-09-15T15:47:53.137Z",
                "type": "response_item",
                "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "call_1",
                            "input": "tools.exec_command({\"cmd\":\"ls -la\"})"},
            },
            {
                "timestamp": "2026-09-15T15:47:53.557Z",
                "type": "response_item",
                "payload": {"type": "custom_tool_call_output", "call_id": "call_1",
                            "output": [{"type": "input_text", "text": "Script completed"}]},
            },
        ])
        events = reader.read("s3")
        assert events[0].role == "assistant"
        assert events[0].kind == "tool_use"
        assert events[0].brief.startswith("exec ")
        assert "ls -la" in events[0].brief
        assert events[1].role == "user"
        assert events[1].kind == "tool_result"

    def test_function_call과_결과를_tool_use_tool_result로_읽는다(self, tmp_path: Path) -> None:
        reader = CodexTranscriptReader(workdir=Path("/w"), home=tmp_path)
        _write_codex_rollout(tmp_path, "s4", [
            {
                "timestamp": "2026-09-05T06:35:26.439Z",
                "type": "response_item",
                "payload": {"type": "function_call", "name": "list_threads", "namespace": "mcp__codex_app",
                            "call_id": "call_2", "arguments": "{\"limit\":100}"},
            },
            {
                "timestamp": "2026-09-05T06:35:26.637Z",
                "type": "response_item",
                "payload": {"type": "function_call_output", "call_id": "call_2", "output": "결과"},
            },
        ])
        events = reader.read("s4")
        assert events[0].kind == "tool_use"
        assert events[0].brief.startswith("mcp__codex_app.list_threads")
        assert events[1].kind == "tool_result"

    def test_token_usage_record가_직전_이벤트에_병합된다(self, tmp_path: Path) -> None:
        """cache_write_input_tokens 를 0이 아닌 값으로 고정한다(코덱스 리뷰
        지적 5번) — 0이면 매핑이 아예 틀려도(엉뚱한 키를 읽어 기존 None 값이
        그대로 남아도) 우연히 0과 구분되지 않을 여지가 있다."""
        reader = CodexTranscriptReader(workdir=Path("/w"), home=tmp_path)
        _write_codex_rollout(tmp_path, "s5", [
            {
                "timestamp": "2026-09-15T15:47:53.137Z",
                "type": "response_item",
                "payload": {"type": "custom_tool_call", "name": "exec", "input": "cmd"},
            },
            {
                "timestamp": "2026-09-15T15:47:53.212Z",
                "type": "token_usage_record",
                "payload": {
                    "response_id": "resp_1",
                    "usage": {
                        "input_tokens": 31738, "cached_input_tokens": 6912,
                        "cache_write_input_tokens": 512, "output_tokens": 494,
                        "reasoning_output_tokens": 103, "total_tokens": 32232,
                    },
                },
            },
        ])
        events = reader.read("s5")
        assert len(events) == 1
        event = events[0]
        assert event.kind == "tool_use"  # usage merges onto the existing event, doesn't add a new one
        assert event.input_tokens == 31738
        assert event.cache_read_tokens == 6912
        assert event.cache_creation_tokens == 512
        assert event.output_tokens == 494
        assert event.request_id == "resp_1"

    def test_시각_역순으로_기록된_사용량도_올바른_이벤트에_붙는다(self, tmp_path: Path) -> None:
        """코덱스 리뷰 지적 6번 — 병합은 파일에 쓰인 순서(직전 이벤트)를 보고
        일어난다. 그 순서가 시각순과 다르면(드물지만 방어 대상) 병합 뒤에
        정렬하던 예전 방식은 usage 를 엉뚱한 이벤트에 붙인다. 이 파일에서
        A(ts=0), B(ts=20) 뒤에 시각상 A 직후인 usage 기록(ts=10)이 온다."""
        reader = CodexTranscriptReader(workdir=Path("/w"), home=tmp_path)
        _write_codex_rollout(tmp_path, "s11", [
            {
                "timestamp": "2026-09-15T15:00:00.000Z",
                "type": "response_item",
                "payload": {"type": "custom_tool_call", "name": "A", "input": "a"},
            },
            {
                "timestamp": "2026-09-15T15:00:20.000Z",
                "type": "response_item",
                "payload": {"type": "custom_tool_call", "name": "B", "input": "b"},
            },
            {
                "timestamp": "2026-09-15T15:00:10.000Z",
                "type": "token_usage_record",
                "payload": {"response_id": "resp-a", "usage": {"input_tokens": 1, "output_tokens": 2}},
            },
        ])
        events = reader.read("s11")
        assert [e.brief for e in events] == ["A a", "B b"]
        a_event, b_event = events
        assert a_event.request_id == "resp-a"
        assert a_event.output_tokens == 2
        assert b_event.request_id is None
        assert b_event.output_tokens is None

    def test_직전_이벤트가_없으면_사용량은_버려진다(self, tmp_path: Path) -> None:
        reader = CodexTranscriptReader(workdir=Path("/w"), home=tmp_path)
        _write_codex_rollout(tmp_path, "s6", [
            {
                "timestamp": "2026-09-15T15:47:53.212Z",
                "type": "token_usage_record",
                "payload": {"response_id": "resp_1", "usage": {"input_tokens": 10, "output_tokens": 5}},
            },
        ])
        assert reader.read("s6") == []

    def test_알수없는_response_item_타입은_건너뛴다(self, tmp_path: Path) -> None:
        reader = CodexTranscriptReader(workdir=Path("/w"), home=tmp_path)
        _write_codex_rollout(tmp_path, "s7", [
            {"timestamp": "2026-09-15T15:47:53.212Z", "type": "response_item", "payload": {"type": "웃긴타입"}},
        ])
        assert reader.read("s7") == []

    def test_event_msg와_world_state는_무시한다(self, tmp_path: Path) -> None:
        reader = CodexTranscriptReader(workdir=Path("/w"), home=tmp_path)
        _write_codex_rollout(tmp_path, "s8", [
            {"timestamp": "2026-09-15T15:47:42.999Z", "type": "event_msg",
             "payload": {"type": "task_started"}},
            {"timestamp": "2026-09-15T15:47:43.000Z", "type": "world_state", "payload": {}},
            {"timestamp": "2026-09-15T15:47:44.000Z", "type": "response_item",
             "payload": {"type": "message", "role": "user", "content": []}},
        ])
        events = reader.read("s8")
        assert len(events) == 1

    def test_시각순으로_정렬한다(self, tmp_path: Path) -> None:
        reader = CodexTranscriptReader(workdir=Path("/w"), home=tmp_path)
        _write_codex_rollout(tmp_path, "s9", [
            {"timestamp": "2026-09-15T15:47:50.000Z", "type": "response_item",
             "payload": {"type": "message", "role": "assistant", "content": []}},
            {"timestamp": "2026-09-15T15:47:40.000Z", "type": "response_item",
             "payload": {"type": "message", "role": "user", "content": []}},
        ])
        events = reader.read("s9")
        assert [e.ts for e in events] == sorted(e.ts for e in events)


class Test기록리더선택_codex:
    def test_codex_는_이제_CodexTranscriptReader_다(self, tmp_path: Path) -> None:
        reader = TranscriptReaderRegistry().create("codex", tmp_path)
        assert isinstance(reader, CodexTranscriptReader)


class TestCodexHome격리:
    """코덱스 리뷰 지적 1번 — CODEX_HOME 격리가 리더에 안 이어지던 문제.

    EngineSpec.home_dir 는 codex 프로필에서 CODEX_HOME 그 자체가 된다
    (engine/environment.py 의 CodexEnvironmentPolicy). 그 자리는
    ~/.codex 를 대체하는 것이지 그 아래 중첩되는 게 아니라서, 리더가
    <CODEX_HOME>/sessions 를 보지 않고 <home>/.codex/sessions 를 보면
    격리 프로필에서 기록을 영영 못 찾는다.
    """

    def test_codex_home이_설정되면_그_자리의_sessions를_본다(self, tmp_path: Path) -> None:
        from slack_cli_agent.config.profile import EngineSpec

        real_home = tmp_path / "real-home"
        codex_home = tmp_path / "bot-codex-home"
        # CODEX_HOME replaces ~/.codex outright — sessions live directly at
        # <codex_home>/sessions, not <codex_home>/.codex/sessions.
        expected = _write_codex_rollout_at(codex_home / "sessions", "격리세션", [
            {"timestamp": "2026-09-16T00:00:00Z", "type": "session_meta", "payload": {}},
        ])
        # 진짜 $HOME 아래에도 같은 세션id로 다른 파일이 있으면 안 된다 —
        # 격리된 자리를 "찾아서" 읽은 것과 "우연히 못 찾아 빈 목록" 을 구분한다.
        assert not (real_home / ".codex").exists()

        spec = EngineSpec(type="codex", binary=Path("codex"), model="m", home_dir=codex_home)
        registry = TranscriptReaderRegistry()
        reader = registry.create("codex", tmp_path / "work", home=real_home, spec=spec)
        assert isinstance(reader, CodexTranscriptReader)
        assert reader.find_transcript_path("격리세션") == expected

    def test_codex_home이_없으면_기존대로_home_아래_codex를_본다(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        expected = _write_codex_rollout(home, "구프로필세션", [
            {"timestamp": "2026-09-16T00:00:00Z", "type": "session_meta", "payload": {}},
        ])
        registry = TranscriptReaderRegistry()
        reader = registry.create("codex", tmp_path / "work", home=home, spec=None)
        assert isinstance(reader, CodexTranscriptReader)
        assert reader.find_transcript_path("구프로필세션") == expected


class TestClaude스펙_home_dir_전달:
    """Claude 도 같은 한계가 있었다 — ClaudeEnvironmentPolicy 의 HOME_ENV_VAR
    는 "HOME" 자체라서, spec.home_dir 이 곧 그 프로세스가 실제로 쓴 $HOME
    이다. registry 가 spec 을 안 받으면 리더는 진짜 사용자 홈을 보게 된다."""

    def test_claude_스펙의_home_dir을_우선한다(self, tmp_path: Path) -> None:
        from slack_cli_agent.config.profile import EngineSpec

        bot_home = tmp_path / "bot-home"
        wrong_home = tmp_path / "wrong-home"
        spec = EngineSpec(type="claude", binary=Path("claude"), model="m", home_dir=bot_home)
        registry = TranscriptReaderRegistry()
        reader = registry.create("claude", Path("/w"), home=wrong_home, spec=spec)
        assert isinstance(reader, ClaudeTranscriptReader)
        assert reader.transcript_path("s1") == (
            bot_home / ".claude" / "projects" / "-w" / "s1.jsonl"
        )

    def test_스펙이_없으면_전달된_home을_그대로_쓴다(self, tmp_path: Path) -> None:
        registry = TranscriptReaderRegistry()
        reader = registry.create("claude", Path("/w"), home=tmp_path, spec=None)
        assert isinstance(reader, ClaudeTranscriptReader)
        assert reader.transcript_path("s1") == tmp_path / ".claude" / "projects" / "-w" / "s1.jsonl"


class Test병합된사용량을_관찰계산이_기대대로_읽는다:
    """코덱스 리뷰 지적 7번 — usage 병합 결과가 실제로
    observability.slow_report 의 detect_retries()/TimeBreakdownCalculator 가
    기대하는 값을 내는지를, 리더가 아니라 그 산출물의 소비자 쪽에서 본다."""

    def test_병합된_usage로_재시도를_감지한다(self, tmp_path: Path) -> None:
        from slack_cli_agent.observability.slow_report import detect_retries

        reader = CodexTranscriptReader(workdir=Path("/w"), home=tmp_path)
        _write_codex_rollout(tmp_path, "retry1", [
            {"timestamp": "2026-09-16T00:00:00.000Z", "type": "response_item",
             "payload": {"type": "message", "role": "assistant", "content": []}},
            {"timestamp": "2026-09-16T00:00:01.000Z", "type": "token_usage_record",
             "payload": {"response_id": "r1", "usage": {
                 "input_tokens": 10, "cached_input_tokens": 50,
                 "cache_write_input_tokens": 100, "output_tokens": 5}}},
            {"timestamp": "2026-09-16T00:00:10.000Z", "type": "response_item",
             "payload": {"type": "message", "role": "assistant", "content": []}},
            {"timestamp": "2026-09-16T00:00:11.000Z", "type": "token_usage_record",
             "payload": {"response_id": "r2", "usage": {
                 "input_tokens": 10, "cached_input_tokens": 200,
                 "cache_write_input_tokens": 0, "output_tokens": 5}}},
        ])
        events = reader.read("retry1")
        retries = detect_retries(events)
        assert len(retries) == 1
        orphan = next(iter(retries.values()))
        assert orphan == 50  # 200 - (50 + 100)

    def test_time_breakdown_calculator가_codex_기록에서_도구_구간을_계산한다(self, tmp_path: Path) -> None:
        from slack_cli_agent.observability.slow_report import TimeBreakdownCalculator

        reader = CodexTranscriptReader(workdir=Path("/w"), home=tmp_path)
        _write_codex_rollout(tmp_path, "breakdown1", [
            {"timestamp": "2026-09-16T00:00:00.000Z", "type": "response_item",
             "payload": {"type": "custom_tool_call", "name": "exec", "input": "ls"}},
            {"timestamp": "2026-09-16T00:00:05.000Z", "type": "response_item",
             "payload": {"type": "custom_tool_call_output",
                         "output": [{"type": "input_text", "text": "결과"}]}},
        ])
        calculator = TimeBreakdownCalculator(assumed_tokens_per_sec=50.0)
        breakdown = calculator.compute(reader, "breakdown1")
        assert breakdown is not None
        assert breakdown.tool_sec == 5.0
