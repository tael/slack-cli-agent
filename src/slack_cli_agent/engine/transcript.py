"""Per-engine session transcript readers, for turn-duration breakdown.

Splits out the file-path resolution and event-parsing part of the
original bot.py's claude_transcript()/time_breakdown(). Since this
package treats the engine as swappable (see engine/base.py), the
Claude Code CLI's jsonl format is kept contained to this module. A
different engine with a different transcript format just needs a
class implementing SessionTranscriptReader; observability/slow_report.py
doesn't need to know what it is.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TranscriptEvent:
    """One parsed line of session transcript, holding only what duration breakdown needs."""

    ts: float
    role: str | None
    kind: str | None  # "tool_use" | "tool_result" | "text" | "thinking" | None
    brief: str
    output_tokens: int | None
    input_tokens: int | None = None
    """Input tokens sent to the model on that turn. Used for session
    context usage (observability.slow_report.SessionContextCalculator)."""
    cache_creation_tokens: int | None = None
    cache_read_tokens: int | None = None
    request_id: str | None = None
    """Used for retry detection (observability.slow_report.detect_retries).

    A dropped-and-retried request leaves no failure event in the
    transcript — it can only be inferred from a discontinuity in cache
    usage. Same three values the original bot.py's time_breakdown()
    (around line 3260) tracked together.
    """


class SessionTranscriptReader(ABC):
    """Contract for reading one session's transcript as a list of events.

    Returns an empty list rather than raising when the transcript is
    missing or unreadable — a duration-breakdown failure shouldn't
    wreck handling of an already-completed request.
    """

    @abstractmethod
    def read(self, session_id: str) -> list[TranscriptEvent]: ...


class ClaudeTranscriptReader(SessionTranscriptReader):
    """Reads the jsonl session transcript the Claude Code CLI writes.

    Path: <home>/.claude/projects/<slugified workdir>/<session_id>.jsonl.
    The slug replaces "/" and "." in the absolute workdir path with
    "-". Same rule as the original bot.py's claude_transcript()
    (line 3076).
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
        """Converts an ISO8601 timestamp to epoch seconds, or None on
        failure. Same as the original bot.py's _parse_iso_ts() (line 3066)."""
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value)).timestamp()
        except (ValueError, TypeError):
            return None


class NullTranscriptReader(SessionTranscriptReader):
    """Always returns an empty list — for engines that don't write a transcript.

    The original didn't even construct a transcript path for the codex
    profile (bot.py:3083), reasoning that it's better than hunting for
    a file that doesn't exist and rendering an empty table. Reusing
    the Claude reader would produce the same empty result, but by
    poking around someone else's home directory.
    """

    def __init__(self, workdir: Path, home: Path | None = None) -> None:
        self._workdir = workdir
        self._home = home

    def read(self, session_id: str) -> list[TranscriptEvent]:
        return []


#: Takes a workdir and a home dir and produces a reader. Home is
#: threaded through separately so tests can set up a transcript
#: without touching the real home dir — without that, "didn't look"
#: and "looked and found nothing" are indistinguishable.
ReaderFactory = Callable[[Path, Path | None], SessionTranscriptReader]


class TranscriptReaderRegistry:
    """Registry of session-transcript readers by engine name.

    Same shape as engine/environment.py's
    EngineEnvironmentPolicyRegistry. Hardcoding engine kinds in code
    would keep callers from plugging in their own engine's transcript
    format.

    An unknown engine gets an empty transcript instead of an
    exception, unlike the environment policy registry, where a missing
    policy is dangerous enough to fail loudly. This one only feeds
    duration breakdown, where a failure shouldn't wreck handling of an
    already-completed request — same reason SessionTranscriptReader's
    contract never raises.
    """

    def __init__(self) -> None:
        # A factory just needs to take a workdir and return a reader —
        # not restricted to a class, since constructor args vary per
        # reader. Whoever registers one wraps that difference in a
        # lambda or partial.
        self._factories: dict[str, ReaderFactory] = {
            "claude": ClaudeTranscriptReader,
            "codex": NullTranscriptReader,
        }

    def register(self, name: str, factory: ReaderFactory) -> None:
        self._factories[name] = factory

    def create(
        self, name: str, workdir: Path, home: Path | None = None
    ) -> SessionTranscriptReader:
        factory = self._factories.get(name, NullTranscriptReader)
        return factory(workdir, home)

    def known_names(self) -> list[str]:
        return sorted(self._factories)
