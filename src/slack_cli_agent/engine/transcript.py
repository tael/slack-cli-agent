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

import inspect
import json
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from .codex import CODEX_USAGE_KEY_MAP

if TYPE_CHECKING:
    from ..config.profile import EngineSpec


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


def _parse_iso_ts(value: Any) -> float | None:
    """Converts an ISO8601 timestamp to epoch seconds, or None on failure.

    Same as the original bot.py's _parse_iso_ts() (line 3066). Shared by
    every reader in this module since every engine's jsonl transcript
    uses the same top-level "timestamp" field and format.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (ValueError, TypeError):
        return None


def _read_timestamped_lines(path: Path) -> list[tuple[float, dict[str, Any]]]:
    """Reads a jsonl file into (timestamp, line) pairs, skipping anything
    unreadable: a missing file, an unreadable one, a broken JSON line, a
    line that isn't an object, or one with no parseable "timestamp".

    Shared by every reader below — the tolerance for partially-broken
    lines is the same regardless of which engine wrote the file.
    """
    if not path.exists():
        return []
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    lines: list[tuple[float, dict[str, Any]]] = []
    for raw_line in text.splitlines():
        try:
            data = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        ts = _parse_iso_ts(data.get("timestamp"))
        if ts is None:
            continue
        lines.append((ts, data))
    return lines


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
        events: list[TranscriptEvent] = []
        # Sorted before parsing, not after — see CodexTranscriptReader.read()'s
        # docstring for why merge-sensitive readers need this order. Claude's
        # reader has no cross-line merge today, but keeping both readers on the
        # same discipline means a future one doesn't have to relearn this.
        for ts, data in sorted(_read_timestamped_lines(path), key=lambda item: item[0]):
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


class CodexTranscriptReader(SessionTranscriptReader):
    """Reads the jsonl rollout file the Codex CLI (`codex exec`) writes.

    Path: <sessions root>/<year>/<month>/<day>/rollout-<started
    at>-<session_id>.jsonl. Confirmed 2026-09-16 against real rollout
    files under ~/.codex/sessions produced by this repo's own `codex
    exec` runs (session_meta.payload.originator == "codex_exec"). The
    date-path prefix isn't derivable from session_id alone, so the
    file is located by searching for a name ending in the session_id
    rather than computed the way ClaudeTranscriptReader.transcript_path
    does.

    The sessions root is <codex_home>/sessions when a profile sets a
    custom CODEX_HOME (engine/environment.py's CodexEnvironmentPolicy),
    since that env var points codex at a directory that replaces
    ~/.codex outright rather than nesting under it — home/.codex/sessions
    would be the wrong root for that profile. Falls back to
    <home>/.codex/sessions, matching ClaudeTranscriptReader's convention,
    for a profile with no isolated CODEX_HOME.

    Unlike Claude, where one line carries both content and usage,
    Codex logs usage as a separate `token_usage_record` line that
    follows the response item(s) from the same model call. _merge_usage
    folds it onto the most recently read event so detect_retries() and
    SessionContextCalculator, both of which only look at role=="assistant"
    events, still see it. Raw lines are sorted by timestamp before this
    merge runs (not after, the way the final list used to be) — the
    merge target is "the assistant event whose ts precedes this usage
    record", and only holds true if lines are visited in chronological
    order. A file whose lines arrive out of ts order would otherwise let
    _merge_usage attach usage to whatever line happened to be read last,
    not the one it actually followed in time.
    """

    def __init__(self, workdir: Path, home: Path | None = None, codex_home: Path | None = None) -> None:
        self._workdir = workdir
        self._home = home or Path.home()
        self._codex_home = codex_home

    def _sessions_dir(self) -> Path:
        if self._codex_home is not None:
            return self._codex_home / "sessions"
        return self._home / ".codex" / "sessions"

    def find_transcript_path(self, session_id: str) -> Path | None:
        sessions_dir = self._sessions_dir()
        if not sessions_dir.is_dir():
            return None
        matches = sorted(sessions_dir.rglob(f"*-{session_id}.jsonl"))
        return matches[-1] if matches else None

    def read(self, session_id: str) -> list[TranscriptEvent]:
        path = self.find_transcript_path(session_id)
        if path is None:
            return []
        events: list[TranscriptEvent] = []
        for ts, data in sorted(_read_timestamped_lines(path), key=lambda item: item[0]):
            top_type = data.get("type")
            payload = data.get("payload")
            if not isinstance(payload, dict):
                continue
            if top_type == "token_usage_record":
                self._merge_usage(events, payload)
                continue
            if top_type != "response_item":
                continue
            event = self._event_from_response_item(ts, payload)
            if event is not None:
                events.append(event)
        return events

    @staticmethod
    def _event_from_response_item(ts: float, payload: Mapping[str, Any]) -> TranscriptEvent | None:
        item_type = payload.get("type")
        if item_type == "message":
            return TranscriptEvent(ts=ts, role=payload.get("role"), kind="text", brief="", output_tokens=None)
        if item_type == "reasoning":
            return TranscriptEvent(ts=ts, role="assistant", kind="thinking", brief="", output_tokens=None)
        if item_type == "custom_tool_call":
            brief = CodexTranscriptReader._tool_call_brief(payload.get("name"), payload.get("input"))
            return TranscriptEvent(ts=ts, role="assistant", kind="tool_use", brief=brief, output_tokens=None)
        if item_type == "function_call":
            brief = CodexTranscriptReader._tool_call_brief(
                payload.get("name"), payload.get("arguments"), namespace=payload.get("namespace"),
            )
            return TranscriptEvent(ts=ts, role="assistant", kind="tool_use", brief=brief, output_tokens=None)
        if item_type in ("custom_tool_call_output", "function_call_output"):
            return TranscriptEvent(ts=ts, role="user", kind="tool_result", brief="", output_tokens=None)
        return None

    @staticmethod
    def _tool_call_brief(name: Any, arg: Any, namespace: Any = None) -> str:
        label = f"{namespace}.{name}" if namespace else str(name or "?")
        return f"{label} {str(arg or '')[:60]}".strip()

    @staticmethod
    def _merge_usage(events: list[TranscriptEvent], payload: Mapping[str, Any]) -> None:
        if not events:
            return
        raw_usage = payload.get("usage")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        if not usage:
            return
        # Same key names as codex.py's CODEX_USAGE_KEY_MAP, read from a
        # different event shape (token_usage_record instead of
        # turn.completed) — sharing the map keeps the two from drifting
        # if Codex ever renames one of these keys.
        output_tokens = usage.get(CODEX_USAGE_KEY_MAP["output_tokens"])
        input_tokens = usage.get(CODEX_USAGE_KEY_MAP["input_tokens"])
        cache_creation = usage.get(CODEX_USAGE_KEY_MAP["cache_creation_tokens"])
        cache_read = usage.get(CODEX_USAGE_KEY_MAP["cache_read_tokens"])
        request_id = payload.get("response_id")
        last = events[-1]
        events[-1] = replace(
            last,
            output_tokens=int(output_tokens) if isinstance(output_tokens, (int, float)) else last.output_tokens,
            input_tokens=int(input_tokens) if isinstance(input_tokens, (int, float)) else last.input_tokens,
            cache_creation_tokens=(
                int(cache_creation) if isinstance(cache_creation, (int, float)) else last.cache_creation_tokens
            ),
            cache_read_tokens=int(cache_read) if isinstance(cache_read, (int, float)) else last.cache_read_tokens,
            request_id=str(request_id) if request_id is not None else last.request_id,
        )


class NullTranscriptReader(SessionTranscriptReader):
    """Always returns an empty list — the default for an unregistered engine name.

    An unregistered name is treated the same as a genuinely
    transcript-less engine: a duration-breakdown failure shouldn't
    wreck handling of an already-completed request (see
    TranscriptReaderRegistry). This is no longer used for "codex" —
    see CodexTranscriptReader — and it's an open question whether it's
    the right choice for "gemini" (sca-dyb.3 checked: the Antigravity
    CLI returns one JSON object per call, not an incremental
    transcript, so there may be nothing to read at all rather than
    something this reader is merely not looking for).
    """

    def __init__(self, workdir: Path, home: Path | None = None, spec: EngineSpec | None = None) -> None:
        self._workdir = workdir
        self._home = home

    def read(self, session_id: str) -> list[TranscriptEvent]:
        return []


#: Takes a workdir, a home dir, and the engine's own EngineSpec (or None,
#: for a caller that doesn't have one) and produces a reader. Home is
#: threaded through separately from spec so tests can set up a transcript
#: without touching the real home dir — without that, "didn't look" and
#: "looked and found nothing" are indistinguishable. spec carries the
#: engine-specific home path (e.g. CODEX_HOME) that Engine's own
#: EngineEnvironmentPolicy actually points the CLI at — without it, a
#: reader for an isolated profile looks in the wrong place, since it
#: never sees where the engine process itself was told to write (sca-kos.1).
ReaderFactory = Callable[[Path, "Path | None", "EngineSpec | None"], SessionTranscriptReader]

#: The shape factories had before spec was added. Still accepted by register().
LegacyReaderFactory = Callable[[Path, "Path | None"], SessionTranscriptReader]


def _adapt_factory(factory: ReaderFactory | LegacyReaderFactory) -> ReaderFactory:
    """Wraps a factory that predates the spec argument so it isn't called with
    three arguments and raised as a TypeError at request time.
    """
    try:
        parameters = list(inspect.signature(factory).parameters.values())
    except (TypeError, ValueError):
        # Builtins and C callables have no introspectable signature; assume current shape.
        return cast(ReaderFactory, factory)
    if any(p.kind is p.VAR_POSITIONAL or p.kind is p.VAR_KEYWORD for p in parameters):
        return cast(ReaderFactory, factory)
    positional = [p for p in parameters if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    if len(positional) >= 3:
        return cast(ReaderFactory, factory)
    legacy = cast(LegacyReaderFactory, factory)
    return lambda workdir, home, spec: legacy(workdir, home)


def _claude_reader_factory(workdir: Path, home: Path | None, spec: EngineSpec | None) -> SessionTranscriptReader:
    # ClaudeEnvironmentPolicy's HOME_ENV_VAR is "HOME" itself (not a
    # claude-specific var), so spec.home_dir *is* the effective $HOME the
    # CLI actually ran under — exactly what ClaudeTranscriptReader's `home`
    # parameter already means. Falls back to the caller-supplied home
    # (defaulting further to Path.home() inside the reader) when the spec
    # sets none, e.g. a bot with no per-bot isolation configured.
    effective_home = spec.home_dir if spec is not None and spec.home_dir is not None else home
    return ClaudeTranscriptReader(workdir, home=effective_home)


def _codex_reader_factory(workdir: Path, home: Path | None, spec: EngineSpec | None) -> SessionTranscriptReader:
    codex_home = spec.home_dir if spec is not None else None
    return CodexTranscriptReader(workdir, home=home, codex_home=codex_home)


def _null_reader_factory(workdir: Path, home: Path | None, spec: EngineSpec | None) -> SessionTranscriptReader:
    return NullTranscriptReader(workdir, home, spec)


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
        # A factory just needs to take (workdir, home, spec) and return a
        # reader — not restricted to a class, since constructor args vary
        # per reader. Whoever registers one wraps that difference in a
        # function, lambda, or partial.
        self._factories: dict[str, ReaderFactory] = {
            "claude": _claude_reader_factory,
            "codex": _codex_reader_factory,
        }

    def register(self, name: str, factory: ReaderFactory | LegacyReaderFactory) -> None:
        self._factories[name] = _adapt_factory(factory)

    def create(
        self, name: str, workdir: Path, home: Path | None = None, *, spec: EngineSpec | None = None,
    ) -> SessionTranscriptReader:
        """spec is keyword-only so it can't be dropped by position. Omitting it
        makes the reader fall back to the default home, which is wrong for an
        isolated profile — pass the EngineSpec of the engine whose transcript
        this is.
        """
        factory = self._factories.get(name, _null_reader_factory)
        return factory(workdir, home, spec)

    def known_names(self) -> list[str]:
        return sorted(self._factories)
