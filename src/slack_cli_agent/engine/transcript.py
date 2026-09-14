"""엔진 세션 기록 파서 — 소요 시간 구간 분해가 읽을 이벤트 목록을 엔진별로 만든다.

원본 bot.py 의 `claude_transcript()`/`time_breakdown()` 앞부분(파일 경로 판정과
이벤트 파싱)을 분리한 것이다. 이 패키지는 엔진이 교체 가능한 범용 어댑터이고
(`engine/base.py` 참조), Claude Code CLI 의 jsonl 형식에 대한 의존은 이 모듈
안에만 가둔다. 다른 엔진이 다른 형식의 세션 기록을 남기면 `SessionTranscriptReader`
계약을 구현하는 클래스를 하나 더 두면 되고, 소요 시간 분해 계산 쪽
(`observability/slow_report.py`)은 그 클래스가 무엇인지 몰라도 된다.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TranscriptEvent:
    """세션 기록 한 줄에서 뽑은 값. 소요 시간 분해에 필요한 것만 담는다."""

    ts: float
    role: str | None
    kind: str | None  # "tool_use" | "tool_result" | "text" | "thinking" | None
    brief: str
    output_tokens: int | None
    input_tokens: int | None = None
    """그 턴이 모델에 보낸 입력 토큰. 세션 컨텍스트 사용량 계산
    (`observability.slow_report.SessionContextCalculator`)에 쓴다."""
    cache_creation_tokens: int | None = None
    cache_read_tokens: int | None = None
    request_id: str | None = None
    """재시도 감지(`observability.slow_report.detect_retries`)에 쓰는 값이다.

    끊겼다 다시 부른 요청은 실패 이벤트 자체가 기록에 안 남고, 캐시 사용량의
    불연속으로만 역산할 수 있다. 원본 bot.py `time_breakdown()` 앞부분
    (3260행 근처)이 이 세 값을 함께 담던 것과 같다.
    """


class SessionTranscriptReader(ABC):
    """세션 하나의 기록을 이벤트 목록으로 돌려주는 계약.

    기록이 없거나 읽지 못하면 빈 목록을 돌려준다. 예외를 밖으로 내지 않는다 —
    소요 시간 분해가 실패해도 이미 끝난 요청 처리 자체를 망치면 안 된다.
    """

    @abstractmethod
    def read(self, session_id: str) -> list[TranscriptEvent]:
        """세션 기록을 시각순으로 정렬해 돌려준다."""


class ClaudeTranscriptReader(SessionTranscriptReader):
    """Claude Code CLI 가 남기는 jsonl 세션 기록을 읽는다.

    경로는 `<home>/.claude/projects/<workdir 슬러그>/<session_id>.jsonl` 이다.
    슬러그는 workdir 절대경로 문자열의 "/" 와 "." 을 "-" 로 바꾼 것이다.
    원본 bot.py `claude_transcript()`(3076행)와 같은 규칙이다.
    """

    def __init__(self, workdir: Path, home: Path | None = None) -> None:
        self._workdir = workdir
        self._home = home or Path.home()

    def transcript_path(self, session_id: str) -> Path:
        slug = str(self._workdir).replace("/", "-").replace(".", "-")
        return self._home / ".claude" / "projects" / slug / f"{session_id}.jsonl"

    def read(self, session_id: str) -> list[TranscriptEvent]:
        path = self.transcript_path(session_id)
        if not path.exists():
            return []
        try:
            text = path.read_text(errors="replace")
        except OSError:
            return []

        events: list[TranscriptEvent] = []
        for line in text.splitlines():
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            ts = self._parse_iso_ts(data.get("timestamp"))
            if ts is None:
                continue
            message = data.get("message")
            if not isinstance(message, dict):
                continue
            kind, brief = self._classify_content(message.get("content"))
            raw_usage = message.get("usage")
            usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
            tokens = usage.get("output_tokens")
            input_tokens = usage.get("input_tokens")
            cache_creation = usage.get("cache_creation_input_tokens")
            cache_read = usage.get("cache_read_input_tokens")
            request_id = data.get("requestId")
            events.append(TranscriptEvent(
                ts=ts,
                role=data.get("type"),
                kind=kind,
                brief=brief,
                output_tokens=int(tokens) if isinstance(tokens, (int, float)) else None,
                input_tokens=int(input_tokens) if isinstance(input_tokens, (int, float)) else None,
                cache_creation_tokens=int(cache_creation) if isinstance(cache_creation, (int, float)) else None,
                cache_read_tokens=int(cache_read) if isinstance(cache_read, (int, float)) else None,
                request_id=str(request_id) if request_id is not None else None,
            ))
        events.sort(key=lambda event: event.ts)
        return events

    @staticmethod
    def _classify_content(content: Any) -> tuple[str | None, str]:
        kind: str | None = None
        brief = ""
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "tool_use":
                    kind = "tool_use"
                    arg = block.get("input") or {}
                    piece = (
                        arg.get("command") or arg.get("file_path") or arg.get("query")
                        or arg.get("pattern") or arg.get("statement") or ""
                    )
                    brief = f"{block.get('name', '?')} {str(piece)[:60]}".strip()
                elif block_type == "tool_result" and kind is None:
                    kind = "tool_result"
                elif block_type == "text" and kind is None:
                    kind = "text"
                elif block_type == "thinking" and kind is None:
                    kind = "thinking"
        return kind, brief

    @staticmethod
    def _parse_iso_ts(value: Any) -> float | None:
        """ISO8601 timestamp 를 epoch 초로 바꾼다. 실패하면 None.

        원본 bot.py `_parse_iso_ts()`(3066행)와 같다.
        """
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value)).timestamp()
        except (ValueError, TypeError):
            return None
