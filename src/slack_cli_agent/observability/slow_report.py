"""Time breakdown for slow requests and reporting to the troubleshooting channel."""

from __future__ import annotations

import itertools
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from ..config.settings import RuntimeSettings
from ..core.timezones import KST
from ..engine.base import Usage
from ..engine.transcript import SessionTranscriptReader, TranscriptEvent
from ..review.base import as_table

#: Maps the name of the engine that answered to the reader for its transcript
#: format. Empty name means the profile's primary engine.
TranscriptReaderResolver = Callable[[str], SessionTranscriptReader]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GapDetail:
    duration_sec: float
    start_ts: float
    end_ts: float
    last_tool_brief: str
    output_tokens: int | None
    think_sec: float
    wait_sec: float
    retry_orphan_tokens: int | None = None
    """Tokens left behind by an aborted attempt, if this gap turned out to be a retry."""


@dataclass(frozen=True)
class TimeBreakdown:
    start_ts: float
    end_ts: float
    tool_sec: float
    think_sec: float
    wait_sec: float
    top_gaps: Sequence[GapDetail] = ()
    retry_sec: float = 0.0
    retries: Mapping[float, int] = field(default_factory=dict)
    """Retry timestamp -> tokens left behind by the aborted attempt. See detect_retries()."""

    @property
    def total_span(self) -> float:
        return max(self.end_ts - self.start_ts, 0.001)  # floor to avoid division by zero


def detect_retries(events: Sequence[TranscriptEvent]) -> dict[float, int]:
    """Infer retried requests from cache usage: a retry reads the cache the aborted
    attempt already wrote, so cache_creation is 0 while cache_read exceeds the prior
    request's read+creation total. There's no direct failure event to key off of, so
    this is reconstructed from usage numbers alone.
    """
    seen: set[str | None] = set()
    reqs: list[tuple[float, int, int]] = []
    for event in events:
        if event.role != "assistant" or event.cache_read_tokens is None or event.request_id in seen:
            continue
        # A request can log the same usage on multiple lines; only the first counts.
        seen.add(event.request_id)
        reqs.append((event.ts, event.cache_creation_tokens or 0, event.cache_read_tokens))

    found: dict[float, int] = {}
    for (_, prev_cache_creation, prev_cache_read), (ts, cache_creation, cache_read) in itertools.pairwise(reqs):
        orphan = cache_read - (prev_cache_read + prev_cache_creation)
        if cache_creation == 0 and orphan > 0:
            found[ts] = orphan
    return found


class TimeBreakdownCalculator:
    """Splits a session's elapsed time into tool execution, thinking, plain wait, and retry.

    A gap right after tool_use ending in tool_result is tool execution. For other
    assistant-turn gaps, output_tokens / assumed_tokens_per_sec estimates the upper
    bound of time explained by token generation (thinking); the remainder is plain
    wait. Gaps with unknown token counts are all plain wait — this is an approximation,
    not a measurement. Wait time inside a gap detect_retries() flags as a retry is
    booked separately, since it's neither thinking nor network latency but time lost
    re-issuing an aborted request.
    """

    def __init__(self, assumed_tokens_per_sec: float, top_n: int = 3) -> None:
        self._assumed_tokens_per_sec = assumed_tokens_per_sec
        self._top_n = top_n

    def compute(
        self, reader: SessionTranscriptReader, session_id: str, since_ts: float | None = None,
    ) -> TimeBreakdown | None:
        # The reader is an argument, not constructor state: which engine answered
        # (and therefore which transcript format to read) is decided per request,
        # and a fallback answer comes from a different engine than the primary.
        try:
            events = reader.read(session_id)
        except Exception as exc:  # noqa: BLE001 — a calc failure must not break request handling
            log.warning("시간 분해용 세션 기록을 읽지 못했다 %s : %s", session_id[:8], exc)
            return None

        if since_ts is not None:
            # Session files accumulate events across requests; drop everything before this request started.
            events = [event for event in events if event.ts >= since_ts]
        if len(events) < 2:
            return None

        start_ts, end_ts = events[0].ts, events[-1].ts
        retries = detect_retries(events)
        tool_sec = 0.0
        think_sec = 0.0
        wait_sec = 0.0
        retry_sec = 0.0
        gaps: list[GapDetail] = []

        prev = events[0]
        last_tool_brief = prev.brief if prev.kind == "tool_use" else ""
        for event in events[1:]:
            duration = max(0.0, event.ts - prev.ts)
            is_tool_result_for_prev_call = (
                prev.role == "assistant" and prev.kind == "tool_use"
                and event.role == "user" and event.kind == "tool_result"
            )
            if is_tool_result_for_prev_call:
                tool_sec += duration
            elif event.role == "assistant":
                if event.output_tokens:
                    think = min(duration, event.output_tokens / self._assumed_tokens_per_sec)
                else:
                    think = 0.0
                wait = duration - think
                think_sec += think
                orphan_tokens = retries.get(event.ts)
                if orphan_tokens:
                    retry_sec += wait
                else:
                    wait_sec += wait
                gaps.append(GapDetail(
                    duration_sec=duration, start_ts=prev.ts, end_ts=event.ts,
                    last_tool_brief=last_tool_brief, output_tokens=event.output_tokens,
                    think_sec=think, wait_sec=wait, retry_orphan_tokens=orphan_tokens,
                ))
            if event.kind == "tool_use":
                last_tool_brief = event.brief
            prev = event

        gaps.sort(key=lambda gap: -gap.duration_sec)
        return TimeBreakdown(
            start_ts=start_ts, end_ts=end_ts, tool_sec=tool_sec,
            think_sec=think_sec, wait_sec=wait_sec, top_gaps=tuple(gaps[: self._top_n]),
            retry_sec=retry_sec, retries=retries,
        )


@dataclass(frozen=True)
class ElapsedDiagnosis:
    diagnosis: str
    elapsed_desc: str


class ElapsedDiagnostician:
    """Detects sleep/disconnect gaps from the difference between wall-clock and monotonic elapsed time."""

    def __init__(self, sleep_gap_suspect_sec: float) -> None:
        self._sleep_gap_suspect_sec = sleep_gap_suspect_sec

    def diagnose(self, elapsed: float, mono_elapsed: float | None) -> ElapsedDiagnosis:
        if mono_elapsed is None:
            return ElapsedDiagnosis(
                diagnosis="판단 불가, 소요 기록에 작업시간 값이 없습니다",
                elapsed_desc=f"{elapsed:.0f}초",
            )
        gap = max(0.0, elapsed - mono_elapsed)
        if gap >= self._sleep_gap_suspect_sec:
            return ElapsedDiagnosis(
                diagnosis=(
                    f"공백 의심, 노트북 잠자기 또는 연결 끊김으로 시계만 돈 구간이 약 {gap:.0f}초 "
                    "섞여 있습니다. 처리 지연이 아닐 가능성이 높습니다"
                ),
                elapsed_desc=f"{elapsed:.0f}초, 실제 작업 {mono_elapsed:.0f}초 + 공백 {gap:.0f}초",
            )
        return ElapsedDiagnosis(diagnosis="실제 처리에 시간이 걸렸습니다", elapsed_desc=f"{elapsed:.0f}초")


@dataclass(frozen=True)
class SlowRequestMeta:
    """Everything the slow-request report is allowed to carry.

    This goes to the troubleshooting channel, which is not the channel the
    request came from, so nothing here may hold engine output or file contents:
    stdout carries the engine's reply body, which can quote the originating
    conversation, a file it read, or a linked thread (sca-r25). Keeping raw out
    of the type rather than filtering at the formatter means a field added later
    can't leak by default.
    """

    elapsed_wall: float
    mono_elapsed: float | None
    started: float | None  # this request's start (since_ts); falls back to the whole session if unset
    model: str
    model_actual: str | None
    effort: str
    num_turns: int | None
    reason: str | None  # e.g. "timeout"
    session_id: str
    resume: bool
    channel: str
    channel_name: str
    text: str
    usage: Usage | None = None
    #: Engine that actually answered. Empty means the profile's primary.
    engine: str = ""


def _fmt_tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.0f}k"
    return str(count)


#: Display order and label for each counted usage field.
_USAGE_LABELS = (
    ("input_tokens", "입력"),
    ("cache_creation_tokens", "캐시 기록"),
    ("cache_read_tokens", "캐시 읽기"),
    ("output_tokens", "출력"),
)


@dataclass(frozen=True)
class SessionContext:
    """Current context usage and limit for a session. Either can be None: `used` when the
    session has no usage recorded yet, `limit` when the model isn't in the context-limit table.
    """

    used: int | None
    limit: int | None


class SessionContextCalculator:
    """Estimates context usage from the last assistant turn's usage — input + cache
    creation + cache read + output is the size the next turn inherits.
    """

    def __init__(self, context_limit: Mapping[str, int]) -> None:
        self._context_limit = context_limit

    def compute(
        self, reader: SessionTranscriptReader, session_id: str, model: str | None = None,
    ) -> SessionContext:
        limit = self._context_limit.get(model or "")
        used: int | None = None
        for event in reader.read(session_id):
            if event.role != "assistant":
                continue
            counts = (
                event.input_tokens,
                event.cache_creation_tokens,
                event.cache_read_tokens,
                event.output_tokens,
            )
            # Skip turns with no usage at all; treating them as 0 would report usage
            # as 0 whenever the last turn happens to lack usage.
            if all(count is None for count in counts):
                continue
            used = sum(count or 0 for count in counts)
        return SessionContext(used=used, limit=limit)


class UsageRowBuilder:
    """Builds token usage rows, but only for owner-only channels — token usage is
    sensitive, so this is enforced in code rather than left to convention.
    """

    def __init__(
        self,
        owner_only_channels: frozenset[str],
        session_context: SessionContextCalculator | None = None,
    ) -> None:
        self._owner_only_channels = owner_only_channels
        self._session_context = session_context

    def build(
        self,
        usage: Usage | None,
        troubleshoot_channel: str,
        *,
        reader: SessionTranscriptReader | None = None,
        session_id: str = "",
        model: str | None = None,
    ) -> list[tuple[str, str]]:
        if troubleshoot_channel not in self._owner_only_channels:
            return []
        rows = [self._token_row(usage)]
        # Without a calculator or a reader, omit the session row entirely rather
        # than guess at 0.
        if self._session_context is not None and reader is not None:
            rows.append(self._session_row(reader, session_id, model))
        return rows

    def _token_row(self, usage: Usage | None) -> tuple[str, str]:
        if usage is None:
            return ("토큰", "기록 없음, 실패로 끝나 사용량이 남지 않았습니다")
        # An engine that doesn't report a field at all (e.g. Gemini's cache
        # creation) marks it unavailable. Printing 0 there would read as a
        # measured zero, and adding it to the total would say the same.
        parts = []
        total = 0
        for name, label in _USAGE_LABELS:
            if name in usage.unavailable:
                parts.append(f"{label} 판정 불가")
                continue
            value = int(getattr(usage, name))
            total += value
            parts.append(f"{label} {_fmt_tokens(value)}")
        detail = " / ".join(parts)
        if usage.unavailable:
            return ("토큰", f"{total:,}개 이상 ({detail})")
        return ("토큰", f"{total:,}개 ({detail})")

    def _session_row(
        self, reader: SessionTranscriptReader, session_id: str, model: str | None,
    ) -> tuple[str, str]:
        assert self._session_context is not None
        context = self._session_context.compute(reader, session_id, model)
        if context.used is None:
            return ("세션", "조회 실패, 세션 기록에 사용량이 없습니다")
        if context.limit:
            ratio = 100 * context.used / context.limit
            return ("세션", f"{_fmt_tokens(context.used)}/{_fmt_tokens(context.limit)} ({ratio:.0f}퍼센트)")
        # Without a known limit, don't compute a ratio — a guessed limit would misjudge how close to exhaustion.
        return ("세션", (
            f"{_fmt_tokens(context.used)} 사용, 한도 미상 "
            f"({model or '?'} 의 맥락 한도를 확인하지 못했습니다)"
        ))


class SlowReportFormatter:
    def __init__(self, assumed_tokens_per_sec: float, now: Callable[[], float] = time.time) -> None:
        self._assumed_tokens_per_sec = assumed_tokens_per_sec
        self._now = now

    def format(
        self,
        meta: SlowRequestMeta,
        diagnosis: ElapsedDiagnosis,
        breakdown: TimeBreakdown | None,
        usage_rows: Sequence[tuple[str, str]] = (),
    ) -> tuple[str, str]:
        rows: list[tuple[str, str]] = [
            ("발생", self._occurred_at(meta)),
            ("대화", meta.channel_name),
            ("진단", diagnosis.diagnosis),
            ("소요", diagnosis.elapsed_desc + (" (상한에 걸려 중단)" if meta.reason == "timeout" else "")),
            *usage_rows,
            ("모델 / effort", self._model_effort_cell(meta)),
            ("세션 이어감", "예" if meta.resume else "아니오 (새 대화)"),
        ]
        if meta.num_turns:
            rows.append(("모델 턴 수", str(meta.num_turns)))
        rows.append(("요청", meta.text[:200]))

        summary = "*느린 요청 트러블슈팅*\n\n" + as_table(rows) + "\n\n시간 분해와 원인은 이 스레드에 이어 답니다."

        detail = self._format_detail(meta, breakdown)
        return summary, detail

    def _occurred_at(self, meta: SlowRequestMeta) -> str:
        # Prefer the request's actual start time; the report time is offset later by the elapsed duration.
        ts = meta.started if meta.started is not None else self._now()
        return datetime.fromtimestamp(ts, KST).strftime("%H:%M:%S") + " KST"

    def _format_detail(self, meta: SlowRequestMeta, breakdown: TimeBreakdown | None) -> str:
        if breakdown is None:
            return "*시간 분해*\n\n계산하지 못했습니다. 세션 기록이 없습니다."

        total_span = breakdown.total_span
        tool_pct = 100 * breakdown.tool_sec / total_span
        think_pct = 100 * breakdown.think_sec / total_span
        wait_pct = 100 * breakdown.wait_sec / total_span
        retry_pct = 100 * breakdown.retry_sec / total_span
        scope = "이번 요청" if meta.started is not None else "세션 전체, 이번 요청 경계 미상"

        lines = [
            "*시간 분해*",
            "",
            (
                f"- 대상 : {scope}, 세션 {meta.session_id[:8]} "
                f"{total_span:.1f}초 ({breakdown.start_ts:.0f}부터 {breakdown.end_ts:.0f}까지)"
            ),
            (
                f"- 사고와 단순 대기는 출력 토큰 수를 초당 {self._assumed_tokens_per_sec:.0f}개로 나눠 "
                "가른 근사치입니다. 실측이 아닙니다"
            ),
            "",
            as_table(
                [
                    ("도구 실행", f"{breakdown.tool_sec:.1f}초", f"{tool_pct:.1f}퍼센트"),
                    ("사고, 토큰 소모", f"{breakdown.think_sec:.1f}초", f"{think_pct:.1f}퍼센트"),
                    ("단순 대기, 토큰 무관", f"{breakdown.wait_sec:.1f}초", f"{wait_pct:.1f}퍼센트"),
                    ("재시도로 버린 시간", f"{breakdown.retry_sec:.1f}초", f"{retry_pct:.1f}퍼센트"),
                ],
                head=("구분", "합계", "비중"),
            ),
        ]

        if breakdown.top_gaps:
            lines += [
                "",
                "*긴 구간*",
                "",
                as_table(
                    [
                        (
                            str(i + 1),
                            f"{gap.duration_sec:.1f}초",
                            gap.last_tool_brief or "-",
                            f"{gap.output_tokens:,}" if gap.output_tokens else "-",
                            f"사고 {gap.think_sec:.1f}초 / 대기 {gap.wait_sec:.1f}초",
                            "재시도" if gap.retry_orphan_tokens else "-",
                        )
                        for i, gap in enumerate(breakdown.top_gaps)
                    ],
                    head=("순위", "소요", "직전 도구", "구간 끝 출력 토큰", "사고/대기 근사", "판정"),
                ),
                "",
                "직전 도구를 처리한 뒤 다음 행동을 정하기까지의 대기입니다.",
            ]

        if breakdown.retries:
            lines += [
                "",
                "*끊겼다 다시 부른 요청*",
                "",
                as_table(
                    [(f"{ts:.0f}", f"{orphan:,}") for ts, orphan in sorted(breakdown.retries.items())],
                    head=("다시 부른 시각(epoch)", "끊긴 시도가 남긴 토큰"),
                ),
                "",
                "캐시 사용량으로 역산한 판정입니다. 실패 이벤트 자체는 기록에 남지 않습니다.",
            ]

        lines += ["", "*원인*", ""]
        # Lead with retry time when present — attributing it to thinking/wait instead
        # would wrongly suggest lowering effort as the fix.
        if retry_pct > 30:
            lines.append(
                f"- 시간 대부분이 끊겼다 다시 부르느라 버린 시간입니다. {breakdown.retry_sec:.1f}초, "
                f"전체의 {retry_pct:.0f}퍼센트입니다. 요청 내용이나 effort 와 무관한 구간입니다."
            )
        elif wait_pct > 50:
            lines.append(
                f"- 시간 대부분이 토큰 생성과 무관한 단순 대기입니다. {breakdown.wait_sec:.1f}초, "
                f"전체의 {wait_pct:.0f}퍼센트입니다."
            )
        elif think_pct > 50:
            lines.append(
                f"- 시간 대부분이 토큰을 실제로 만들어내는 사고 시간입니다. {breakdown.think_sec:.1f}초, "
                f"전체의 {think_pct:.0f}퍼센트입니다."
            )
        elif tool_pct > 30:
            lines.append(
                f"- 도구 실행 자체가 {breakdown.tool_sec:.1f}초, 전체의 {tool_pct:.0f}퍼센트를 차지합니다."
            )
        else:
            lines.append("- 특정 구간에 시간이 몰리지 않고 고르게 분산돼 있습니다.")

        return "\n".join(lines)

    @staticmethod
    def _model_effort_cell(meta: SlowRequestMeta) -> str:
        shown = meta.model if not meta.model_actual or meta.model_actual == meta.model else (
            f"{meta.model} (실제 {meta.model_actual})"
        )
        return f"{shown} / {meta.effort or '?'}"


class SlowReportPublisher(Protocol):
    def post(self, channel: str, thread_ts: Any, text: str, rich: bool) -> str | None: ...


class SlowRequestReporter:
    """Opens a new thread in the troubleshooting channel for one slow request.
    Any failure here is swallowed — a reporting failure must not undo an
    already-completed request.
    """

    def __init__(
        self,
        *,
        publisher: SlowReportPublisher,
        calculator: TimeBreakdownCalculator,
        diagnostician: ElapsedDiagnostician,
        formatter: SlowReportFormatter,
        settings: RuntimeSettings,
        troubleshoot_channel: str,
        readers: TranscriptReaderResolver,
        usage_row_builder: UsageRowBuilder | None = None,
    ) -> None:
        self._publisher = publisher
        self._calculator = calculator
        self._readers = readers
        self._diagnostician = diagnostician
        self._formatter = formatter
        self._settings = settings
        self._troubleshoot_channel = troubleshoot_channel
        self._usage_row_builder = usage_row_builder or UsageRowBuilder(settings.owner_only_channels)
        # The report carries the request text, the channel name and the session
        # id. Outside an owner-only channel that reaches everyone in it, so the
        # decision is made here at assembly rather than per post -- a config
        # mistake should be visible at startup, not only in what stops arriving.
        self._owner_only = (
            not troubleshoot_channel or troubleshoot_channel in settings.owner_only_channels
        )
        if not self._owner_only:
            log.warning(
                "느린 요청 보고를 끕니다 : 트러블슈팅 채널 %s 이 소유자 전용이 아닙니다. "
                "settings 의 owner_only_channels 에 넣으면 보고가 다시 나갑니다.",
                troubleshoot_channel,
            )

    def maybe_report(self, meta: SlowRequestMeta) -> str | None:
        if meta.elapsed_wall < self._settings.slow_report_sec:
            return None
        if not self._troubleshoot_channel:
            return None
        if not self._owner_only:
            return None

        try:
            return self._report(meta)
        except Exception as exc:  # noqa: BLE001 — a reporting failure must not clobber the request result
            log.error("느린 요청 보고 실패 : %s", exc)
            return None

    def _report(self, meta: SlowRequestMeta) -> str | None:
        diagnosis = self._diagnostician.diagnose(meta.elapsed_wall, meta.mono_elapsed)
        reader = self._readers(meta.engine)
        breakdown = self._calculator.compute(reader, meta.session_id, since_ts=meta.started)
        usage_rows = self._usage_row_builder.build(
            meta.usage, self._troubleshoot_channel,
            reader=reader, session_id=meta.session_id, model=meta.model,
        )
        summary, detail = self._formatter.format(meta, diagnosis, breakdown, usage_rows=usage_rows)

        parent_ts = self._publisher.post(self._troubleshoot_channel, None, summary, rich=True)
        if parent_ts:
            self._publisher.post(self._troubleshoot_channel, parent_ts, detail, rich=True)
        else:
            log.error("느린 요청 본문 게시에 실패해 시간 분해를 올리지 않았다")
        return parent_ts


__all__ = [
    "ElapsedDiagnosis",
    "ElapsedDiagnostician",
    "GapDetail",
    "SessionContext",
    "SessionContextCalculator",
    "SlowReportFormatter",
    "SlowRequestMeta",
    "SlowRequestReporter",
    "TimeBreakdown",
    "TimeBreakdownCalculator",
    "TranscriptEvent",
    "TranscriptReaderResolver",
    "UsageRowBuilder",
    "detect_retries",
]
