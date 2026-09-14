"""engine/transcript.py 의 세션 기록 파서 테스트.

원본 bot.py 의 `claude_transcript()`/`time_breakdown()` 앞부분(이벤트 파싱)을
분리한 `ClaudeTranscriptReader` 를 검증한다. jsonl 형식 의존은 이 클래스
안에 갇혀 있어야 하고, 호출부는 `TranscriptEvent` 목록만 본다.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from slack_cli_agent.engine.transcript import (
    ClaudeTranscriptReader,
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
        class 남의리더(SessionTranscriptReader):
            def __init__(self, workdir: Path, home: Path | None = None) -> None:
                self.workdir = workdir

            def read(self, session_id: str) -> list[TranscriptEvent]:
                return []

        registry = TranscriptReaderRegistry()
        registry.register("남의엔진", 남의리더)
        assert isinstance(registry.create("남의엔진", tmp_path), 남의리더)

    def test_등록소끼리_서로_영향이_없다(self, tmp_path: Path) -> None:
        class 남의리더(SessionTranscriptReader):
            def __init__(self, workdir: Path, home: Path | None = None) -> None:
                self.workdir = workdir

            def read(self, session_id: str) -> list[TranscriptEvent]:
                return []

        first = TranscriptReaderRegistry()
        first.register("남의엔진", 남의리더)
        assert not isinstance(TranscriptReaderRegistry().create("남의엔진", tmp_path), 남의리더)
