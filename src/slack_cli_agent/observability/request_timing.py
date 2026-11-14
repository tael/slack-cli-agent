"""Per-request time attribution for the audit row (S0 계측).

The slow-report breakdown (slow_report.TimeBreakdownCalculator) splits the
span between the first and last transcript line of a request into tool /
think / wait. It explained 21.6 percent of a 959 second request with 0.2
seconds of wait, because most of that request sat outside the span it looks
at: before the engine wrote its first line, which no item covered. It also
drops intervals between two consecutive tool results, books a subagent or
MCP call as an ordinary tool, and has no item for the finish review's Stop
hook rewrites.

This module partitions the request's wall time instead. Every second from
the handler's start to the engine's return lands in exactly one item, so the
items add up to `elapsed` by construction and a gap shows up as a named item
(or as `unattributed_sec` when there is no transcript), never as nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ..engine.transcript import SessionTranscriptReader, TimingEvent

log = logging.getLogger(__name__)

#: Tool names whose call is a wait on a subagent, not a tool run.
SUBAGENT_TOOLS = frozenset({"Task", "Agent"})
#: Claude Code names MCP tools mcp__<server>__<tool>.
MCP_TOOL_PREFIX = "mcp__"

#: Item order in the audit row. Everything but queue_wait_sec sits inside
#: elapsed; queue wait is before the handler starts, so it is reported next
#: to the parts rather than among them.
PART_KEYS = (
    "pre_engine_sec",
    "startup_sec",
    "model_api_sec",
    "tool_sec",
    "subagent_sec",
    "mcp_sec",
    "hook_sec",
    "finish_review_sec",
    "other_sec",
    "unattributed_sec",
)


def tool_category(name: str) -> str:
    if name in SUBAGENT_TOOLS:
        return "subagent_sec"
    if name.startswith(MCP_TOOL_PREFIX):
        return "mcp_sec"
    return "tool_sec"


@dataclass(frozen=True)
class RequestTiming:
    """Where one request's wall time went. Seconds, all items non-negative.

    - pre_engine_sec: handler start to the last engine launch. Prompt build,
      transcription, and a failed first attempt before a session reset.
    - startup_sec: engine launch to its first model request. Process spawn,
      MCP server start, session load.
    - model_api_sec: waiting on the model with no tool call open.
    - tool_sec / subagent_sec / mcp_sec: a tool call open. Parallel calls are
      booked once, to the slowest kind open (subagent, then MCP, then tool).
    - hook_sec: hook run time as the engine measured it.
    - finish_review_sec: everything after the finish review's Stop hook sent
      the model back, i.e. the rewrite rounds.
    - other_sec: engine's last transcript line to its return, and handler work
      after it that elapsed still covers.
    - unattributed_sec: engine time with no transcript to split it.
    """

    elapsed_sec: float
    queue_wait_sec: float | None = None
    pre_engine_sec: float = 0.0
    startup_sec: float = 0.0
    model_api_sec: float = 0.0
    tool_sec: float = 0.0
    subagent_sec: float = 0.0
    mcp_sec: float = 0.0
    hook_sec: float = 0.0
    finish_review_sec: float = 0.0
    other_sec: float = 0.0
    unattributed_sec: float = 0.0
    finish_review_blocks: int = 0
    wall_minus_mono_sec: float | None = None
    """Wall elapsed minus monotonic elapsed. Large means the machine slept or
    the process was stopped, not that any item above was slow."""
    transcript: bool = False

    @property
    def parts_sum(self) -> float:
        return sum(getattr(self, key) for key in PART_KEYS)

    def as_audit_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {key: round(getattr(self, key), 2) for key in PART_KEYS}
        out["queue_wait_sec"] = round(self.queue_wait_sec, 2) if self.queue_wait_sec is not None else None
        out["parts_sum_sec"] = round(self.parts_sum, 2)
        out["elapsed_sec"] = round(self.elapsed_sec, 2)
        out["finish_review_blocks"] = self.finish_review_blocks
        if self.wall_minus_mono_sec is not None:
            out["wall_minus_mono_sec"] = round(self.wall_minus_mono_sec, 2)
        out["transcript"] = self.transcript
        return out


class _Buckets:
    """Running totals plus the last interval's bucket, so a hook reported on
    the line after its run can take its time out of that interval."""

    def __init__(self) -> None:
        self.totals: dict[str, float] = dict.fromkeys(PART_KEYS, 0.0)
        self._last_key = ""
        self._last_left = 0.0

    def add(self, key: str, sec: float) -> None:
        if sec <= 0:
            return
        self.totals[key] += sec
        self._last_key, self._last_left = key, sec

    def move_to_hook(self, sec: float) -> None:
        moved = min(sec, self._last_left)
        if moved <= 0 or not self._last_key or self._last_key == "hook_sec":
            return
        self.totals[self._last_key] -= moved
        self.totals["hook_sec"] += moved
        self._last_left -= moved


def attribute(
    events: Sequence[TimingEvent],
    *,
    start: float,
    engine_start: float,
    engine_end: float,
    end: float | None = None,
    queue_wait_sec: float | None = None,
    mono_elapsed: float | None = None,
) -> RequestTiming:
    """Partitions [start, end] into RequestTiming items.

    `end` defaults to engine_end. Events outside [engine_start, engine_end]
    belong to another request in the same session file and are ignored.
    """
    end = engine_end if end is None else end
    engine_start = min(max(engine_start, start), end)
    engine_end = min(max(engine_end, engine_start), end)
    elapsed = max(0.0, end - start)
    buckets = _Buckets()
    buckets.add("pre_engine_sec", engine_start - start)

    window = sorted(
        (event for event in events if engine_start <= event.ts <= engine_end),
        key=lambda event: event.ts,
    )
    blocks = 0
    if not window:
        buckets.add("unattributed_sec", engine_end - engine_start)
    else:
        # Startup ends at the first model request. An engine whose transcript
        # has no request lines starts counting at its first line instead.
        first = next((i for i, event in enumerate(window) if event.kind == "api_request"), 0)
        cursor = window[first].ts
        buckets.add("startup_sec", cursor - engine_start)
        for event in window[:first]:
            if event.hook_sec:
                buckets.move_to_hook(event.hook_sec)
        open_calls: dict[str, str] = {}
        in_review = False
        for event in window[first:]:
            gap = event.ts - cursor
            if gap > 0:
                buckets.add(_bucket_for(open_calls, in_review), gap)
                cursor = event.ts
            if event.hook_sec:
                buckets.move_to_hook(event.hook_sec)
            if event.kind == "stop_block":
                in_review = True
                blocks += 1
            for call_id, name in event.tool_uses:
                open_calls[call_id or f"#anon{len(open_calls)}"] = tool_category(name)
            if event.kind == "tool_result":
                _close(open_calls, event.tool_results)
        buckets.add("other_sec", engine_end - cursor)
    buckets.add("other_sec", end - engine_end)

    totals = buckets.totals
    return RequestTiming(
        elapsed_sec=elapsed,
        queue_wait_sec=queue_wait_sec,
        finish_review_blocks=blocks,
        wall_minus_mono_sec=(max(0.0, elapsed - mono_elapsed) if mono_elapsed is not None else None),
        transcript=bool(window),
        **{key: max(0.0, totals[key]) for key in PART_KEYS},
    )


def _bucket_for(open_calls: dict[str, str], in_review: bool) -> str:
    if in_review:
        return "finish_review_sec"
    if not open_calls:
        return "model_api_sec"
    kinds = set(open_calls.values())
    for key in ("subagent_sec", "mcp_sec"):
        if key in kinds:
            return key
    return "tool_sec"


def _close(open_calls: dict[str, str], ids: Sequence[str]) -> None:
    known = [call_id for call_id in ids if call_id in open_calls]
    if known:
        for call_id in known:
            open_calls.pop(call_id, None)
    elif open_calls:
        # No id to match (a reader without call ids, or an id the call line
        # lacked): close the oldest open call.
        open_calls.pop(next(iter(open_calls)))


class RequestTimingCalculator:
    """Reads the session transcript and attributes one request's time.

    Never raises: the audit row is written after the answer went out, and a
    failure here must not lose the row. On a read failure the engine time
    goes to unattributed_sec rather than to nothing.
    """

    def __init__(self, readers: Callable[[str], SessionTranscriptReader]) -> None:
        self._readers = readers

    def compute(
        self,
        *,
        engine: str,
        session_id: str,
        start: float,
        engine_start: float,
        engine_end: float,
        end: float | None = None,
        queue_wait_sec: float | None = None,
        mono_elapsed: float | None = None,
    ) -> RequestTiming:
        events: Sequence[TimingEvent] = ()
        if session_id:
            try:
                events = self._readers(engine).read_timing(session_id, since_ts=engine_start)
            except Exception as exc:  # noqa: BLE001 — the audit row must still be written
                log.warning("시간 분해용 세션 기록을 읽지 못했다 %s : %s", session_id[:8], exc)
        return attribute(
            events, start=start, engine_start=engine_start, engine_end=engine_end, end=end,
            queue_wait_sec=queue_wait_sec, mono_elapsed=mono_elapsed,
        )


__all__ = [
    "MCP_TOOL_PREFIX",
    "PART_KEYS",
    "SUBAGENT_TOOLS",
    "RequestTiming",
    "RequestTimingCalculator",
    "attribute",
    "tool_category",
]
