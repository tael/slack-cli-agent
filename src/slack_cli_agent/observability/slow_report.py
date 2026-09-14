"""느린 요청 소요 시간 구간별 분해와 트러블슈팅 채널 보고.

원본 bot.py 의 `time_breakdown()`(3227행 근처), `diagnose_elapsed()`(3142행),
`report_slow()`(3467행 근처)를 재구성했다. 계산(`TimeBreakdownCalculator`)과
발신(`SlowRequestReporter`)을 분리했다 — 구간 분해는 세션 기록만 있으면
슬랙 없이 시험할 수 있어야 하고, 발신은 `slack.publisher.MessagePublisher`
계열에 맡긴다.

엔진 세션 기록의 형식 의존은 `engine.transcript.SessionTranscriptReader` 가
가둔다. 이 모듈은 그 계약이 돌려준 `TranscriptEvent` 목록만 본다 — 엔진이
바뀌어도 이 모듈은 손댈 필요가 없다.
"""

from __future__ import annotations

import itertools
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from ..config.settings import RuntimeSettings
from ..engine.base import Usage
from ..engine.transcript import SessionTranscriptReader, TranscriptEvent
from ..review.base import as_table

log = logging.getLogger(__name__)

# 보고에 적는 시각의 기준. 원본 bot.py 의 KST 와 같다.
KST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class GapDetail:
    """assistant 턴 하나의 구간 상세. 상위 구간 표에 쓴다."""

    duration_sec: float
    start_ts: float
    end_ts: float
    last_tool_brief: str
    output_tokens: int | None
    think_sec: float
    wait_sec: float
    retry_orphan_tokens: int | None = None
    """이 구간이 재시도로 판정됐으면 끊긴 시도가 남긴 토큰 수. 아니면 None."""


@dataclass(frozen=True)
class TimeBreakdown:
    """시간 분해 계산 결과. 값 객체이고 슬랙을 모른다."""

    start_ts: float
    end_ts: float
    tool_sec: float
    think_sec: float
    wait_sec: float
    top_gaps: Sequence[GapDetail] = ()
    retry_sec: float = 0.0
    """끊겼다 다시 부른 요청이 버린 시간 합계. 재시도가 없으면 0.0."""
    retries: Mapping[float, int] = field(default_factory=dict)
    """{다시 부른 시각 : 끊긴 시도가 남긴 토큰 수}. `detect_retries()` 결과다."""

    @property
    def total_span(self) -> float:
        # 0 으로 나누는 사고를 막는 하한. 원본과 같다.
        return max(self.end_ts - self.start_ts, 0.001)


def detect_retries(events: Sequence[TranscriptEvent]) -> dict[float, int]:
    """세션 기록의 캐시 사용량으로 끊겼다 다시 부른 요청을 찾아낸다.

    스트림이 중간에 끊기고 같은 요청을 다시 부르면, 끊긴 시도가 이미 만들어
    둔 캐시를 다음 시도가 읽기만 한다. 그래서 다시 부른 요청은 캐시 기록이
    0인데 캐시 읽기는 직전 요청의 (읽기 + 기록) 합보다 커진다. 그 차이가
    끊긴 시도가 남긴 몫이다. 이 두 조건이 함께 성립하는 요청을 재시도로 본다.

    실패 이벤트 자체는 어디에도 남지 않아 사용량으로 역산하는 수밖에 없다.
    원본 bot.py `detect_retries()`(3196행)와 같은 규칙이다.

    {끝난 시각 : 끊긴 시도가 남긴 토큰 수} 를 돌려준다.
    """
    seen: set[str | None] = set()
    reqs: list[tuple[float, int, int]] = []
    for event in events:
        if event.role != "assistant" or event.cache_read_tokens is None or event.request_id in seen:
            continue
        # 한 요청이 블록마다 같은 사용량을 여러 줄로 남긴다. 첫 줄만 센다.
        seen.add(event.request_id)
        reqs.append((event.ts, event.cache_creation_tokens or 0, event.cache_read_tokens))

    found: dict[float, int] = {}
    for (_, prev_cache_creation, prev_cache_read), (ts, cache_creation, cache_read) in itertools.pairwise(reqs):
        orphan = cache_read - (prev_cache_read + prev_cache_creation)
        if cache_creation == 0 and orphan > 0:
            found[ts] = orphan
    return found


class TimeBreakdownCalculator:
    """세션 기록에서 도구 실행·사고·단순 대기·재시도 구간을 가른다.

    구분 방법 — tool_use 바로 뒤에 오는 tool_result 까지의 간격은 도구
    실행이다. 그 밖의 assistant 턴 간격은, 그 턴이 낸 출력 토큰 수를
    `assumed_tokens_per_sec` 로 나눠 "토큰 생성으로 설명되는 시간"의 상한을
    어림하고 그것을 사고 시간으로, 넘는 나머지를 단순 대기로 본다. 토큰
    수를 모르는 구간은 전부 단순 대기다. 근사치이지 실측이 아니다.

    다만 `detect_retries()` 가 재시도로 판정한 구간의 단순 대기는 별도로
    잡는다. 이 시간은 사고도 네트워크 지연도 아니고 끊긴 시도를 다시
    부르느라 버린 시간이라, 원인을 effort 나 대기로 돌리면 안 되기 때문이다.
    """

    def __init__(
        self,
        transcript_reader: SessionTranscriptReader,
        assumed_tokens_per_sec: float,
        top_n: int = 3,
    ) -> None:
        self._reader = transcript_reader
        self._assumed_tokens_per_sec = assumed_tokens_per_sec
        self._top_n = top_n

    def compute(self, session_id: str, since_ts: float | None = None) -> TimeBreakdown | None:
        try:
            events = self._reader.read(session_id)
        except Exception as exc:  # noqa: BLE001 — 계산 실패가 요청 처리를 망치면 안 된다
            log.warning("시간 분해용 세션 기록을 읽지 못했다 %s : %s", session_id[:8], exc)
            return None

        if since_ts is not None:
            # 이전 요청들의 이벤트를 끊어낸다. 세션이 이어질수록 파일에
            # 이벤트가 쌓이므로, 이번 요청 시작 이전 것은 전부 무관한 과거다.
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
    """소요시간이 실제 작업인지 공백인지를 가른 결과."""

    diagnosis: str
    elapsed_desc: str


class ElapsedDiagnostician:
    """벽시계 소요와 monotonic 소요의 차이로 공백(잠자기·연결 끊김)을 가른다.

    원본 bot.py `diagnose_elapsed()`(3142행)와 같다.
    """

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
    """느린 요청 보고 한 건에 필요한 값. 원본 meta dict 에 해당한다."""

    elapsed_wall: float
    mono_elapsed: float | None
    started: float | None  # 이번 요청 시작 시각(since_ts). 없으면 세션 전체로 넓게 잡는다
    model: str
    model_actual: str | None
    effort: str
    num_turns: int | None
    reason: str | None  # "timeout" 등
    session_id: str
    resume: bool
    channel: str
    channel_name: str
    text: str
    usage: Usage | None = None
    """이번 요청 한 건이 쓴 토큰. `UsageRowBuilder` 가 표 행으로 바꾼다."""
    stdout_tail: str | None = None
    stderr_tail: str | None = None
    """엔진이 비정상 종료했을 때 남긴 표준 출력·오류 꼬리. `tail_output()` 으로
    잘라 담는다. 없으면(정상 종료 등) None 이고, 보고에서 그 블록을 아예 뺀다."""


def tail_output(buf: str | bytes | None, limit: int = 800) -> str:
    """엔진 출력에서 마지막 `limit` 글자만 남긴다. 없으면 빈 문자열이다.

    원본 bot.py `tail_output()`(1491행)과 같다. `EngineResponse.raw` 에 담긴
    표준 출력·오류(`stdout`/`stderr` 키)를 `SlowRequestMeta.stdout_tail`/
    `stderr_tail` 로 옮길 때 이 함수를 쓴다.
    """
    if not buf:
        return ""
    text = buf.decode("utf-8", "replace") if isinstance(buf, bytes) else buf
    return text.strip()[-limit:]


def _fmt_tokens(count: int) -> str:
    """토큰 수를 읽기 쉬운 단위로 줄인다. 1000 미만은 그대로 센다.

    원본 bot.py `fmt_tokens()`(3345행 근처)와 같다.
    """
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.0f}k"
    return str(count)


@dataclass(frozen=True)
class SessionContext:
    """그 세션이 지금 쓰는 컨텍스트 크기와 한도.

    둘 다 없을 수 있다. 사용량은 세션 기록이 없거나 usage 가 한 번도 안
    남았을 때 None 이고, 한도는 `RuntimeSettings.context_limit` 표에 그
    모델이 없을 때 None 이다.
    """

    used: int | None
    limit: int | None


class SessionContextCalculator:
    """세션 기록의 마지막 assistant 턴 usage 로 컨텍스트 사용량을 계산한다.

    원본 bot.py `session_context()`(3384행)와 같다. 마지막 턴의 입력·캐시
    기록·캐시 읽기·출력을 더한 값이 다음 턴이 이어받는 크기이고, 그것이 지금
    상태에 가장 가깝다.

    세션 기록 형식 의존은 `SessionTranscriptReader` 가 가둔다 — 구간 분해가
    쓰는 파서를 그대로 재사용한다.
    """

    def __init__(self, transcript_reader: SessionTranscriptReader, context_limit: Mapping[str, int]) -> None:
        self._reader = transcript_reader
        self._context_limit = context_limit

    def compute(self, session_id: str, model: str | None = None) -> SessionContext:
        limit = self._context_limit.get(model or "")
        used: int | None = None
        for event in self._reader.read(session_id):
            if event.role != "assistant":
                continue
            counts = (
                event.input_tokens,
                event.cache_creation_tokens,
                event.cache_read_tokens,
                event.output_tokens,
            )
            # usage 가 아예 없는 턴은 건너뛴다. 0 으로 채워 더하면 마지막
            # 턴이 usage 없는 턴일 때 사용량이 0 으로 보고된다.
            if all(count is None for count in counts):
                continue
            used = sum(count or 0 for count in counts)
        return SessionContext(used=used, limit=limit)


class UsageRowBuilder:
    """토큰 사용량 행을 만든다. 소유자 전용 채널이 아니면 아무것도 내지 않는다.

    원본 bot.py `usage_rows()`(3420행)와 같은 계약이다. 토큰 사용 현황은
    민감정보라, 보고를 올리는 채널(트러블슈팅 채널)이 소유자 전용 채널
    집합에 있을 때만 낸다. 이 판정을 코드로 못 박아, 지침으로만 두면 다른
    채널로 보고를 옮겼을 때 조용히 새는 것을 막는다.
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
        session_id: str = "",
        model: str | None = None,
    ) -> list[tuple[str, str]]:
        if troubleshoot_channel not in self._owner_only_channels:
            return []
        rows = [self._token_row(usage)]
        # 계산기를 안 주면 세션 행을 아예 안 낸다. 세션 기록 경로가 없는
        # 조립 코드에서 0 이나 추정값을 내는 것보다 행이 없는 편이 정확하다.
        if self._session_context is not None:
            rows.append(self._session_row(session_id, model))
        return rows

    def _token_row(self, usage: Usage | None) -> tuple[str, str]:
        if usage is None:
            return ("토큰", "기록 없음, 실패로 끝나 사용량이 남지 않았습니다")
        total = usage.input_tokens + usage.cache_creation_tokens + usage.cache_read_tokens + usage.output_tokens
        detail = (
            f"입력 {_fmt_tokens(usage.input_tokens)} / 캐시 기록 {_fmt_tokens(usage.cache_creation_tokens)} / "
            f"캐시 읽기 {_fmt_tokens(usage.cache_read_tokens)} / 출력 {_fmt_tokens(usage.output_tokens)}"
        )
        return ("토큰", f"{total:,}개 ({detail})")

    def _session_row(self, session_id: str, model: str | None) -> tuple[str, str]:
        assert self._session_context is not None
        context = self._session_context.compute(session_id, model)
        if context.used is None:
            return ("세션", "조회 실패, 세션 기록에 사용량이 없습니다")
        if context.limit:
            ratio = 100 * context.used / context.limit
            return ("세션", f"{_fmt_tokens(context.used)}/{_fmt_tokens(context.limit)} ({ratio:.0f}퍼센트)")
        # 한도를 모르면 비율을 내지 않는다. 추측한 한도로 만든 퍼센트는
        # 컨텍스트 소진 임박 판정을 틀리게 한다.
        return ("세션", (
            f"{_fmt_tokens(context.used)} 사용, 한도 미상 "
            f"({model or '?'} 의 맥락 한도를 확인하지 못했습니다)"
        ))


class SlowReportFormatter:
    """진단·구간 분해 계산 결과를 슬랙에 올릴 요약·상세 문자열로 만든다.

    표 조립은 `review.base.as_table` 을 그대로 쓴다 — 이 패키지 안에 표
    생성 로직을 또 두지 않는다.
    """

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
        """요청이 일어난 시각. 원본 `report_slow()` rows 첫 항목이다.

        요청 시작 시각이 있으면 그것을 쓴다. 원본은 보고를 만드는 시각을
        적었는데, 그 값은 이미 경과한 소요만큼 실제 발생 시점보다 뒤다.
        시작 시각이 없는 경우에만 보고 시각으로 되돌린다.
        """
        ts = meta.started if meta.started is not None else self._now()
        return datetime.fromtimestamp(ts, KST).strftime("%H:%M:%S") + " KST"

    def _format_detail(self, meta: SlowRequestMeta, breakdown: TimeBreakdown | None) -> str:
        if breakdown is None:
            lines = ["*시간 분해*", "", "계산하지 못했습니다. 세션 기록이 없습니다."]
            lines += self._tail_lines(meta)
            return "\n".join(lines)

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
        # 재시도가 잡히면 그 시간을 먼저 짚는다. 사고나 대기로 설명하면
        # effort 를 낮추라는 엉뚱한 처방으로 이어진다.
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

        lines += self._tail_lines(meta)

        return "\n".join(lines)

    @staticmethod
    def _tail_lines(meta: SlowRequestMeta) -> list[str]:
        """표준 오류·출력 꼬리 블록. 값이 없는 쪽은 아예 안 낸다.

        원본 bot.py `report_slow()` 끝부분(3601행 근처)과 같다. 필드에는
        `tail_output()` 으로 이미 800자까지 잘라 담아 두므로, 여기서는
        표시용으로 마지막 600자만 더 좁힌다.
        """
        lines: list[str] = []
        for label, tail in (("표준 오류", meta.stderr_tail), ("표준 출력", meta.stdout_tail)):
            if tail:
                lines += ["", f"*{label} 끝부분*", "```", tail[-600:], "```"]
        return lines

    @staticmethod
    def _model_effort_cell(meta: SlowRequestMeta) -> str:
        shown = meta.model if not meta.model_actual or meta.model_actual == meta.model else (
            f"{meta.model} (실제 {meta.model_actual})"
        )
        return f"{shown} / {meta.effort or '?'}"


class SlowReportPublisher(Protocol):
    """`slack.publisher.MessagePublisher` 가 만족하는 좁은 계약."""

    def post(self, channel: str, thread_ts: Any, text: str, rich: bool) -> str | None: ...


class SlowRequestReporter:
    """느린 요청 하나를 트러블슈팅 채널에 새 스레드로 연다.

    원본 `report_slow()` 에 해당한다. 계산(`TimeBreakdownCalculator`)과
    발신(`SlowReportPublisher` 계열)을 조립만 하고, 실패는 무엇이든 삼킨다 —
    보고가 실패해도 이미 끝난 요청 처리 자체를 망치면 안 된다.
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
        usage_row_builder: UsageRowBuilder | None = None,
    ) -> None:
        self._publisher = publisher
        self._calculator = calculator
        self._diagnostician = diagnostician
        self._formatter = formatter
        self._settings = settings
        self._troubleshoot_channel = troubleshoot_channel
        self._usage_row_builder = usage_row_builder or UsageRowBuilder(settings.owner_only_channels)

    def maybe_report(self, meta: SlowRequestMeta) -> str | None:
        """기준값을 넘고 보고 채널이 있으면 보고한다. 그 밖엔 조용히 넘어간다."""
        if meta.elapsed_wall < self._settings.slow_report_sec:
            return None
        if not self._troubleshoot_channel:
            return None

        try:
            return self._report(meta)
        except Exception as exc:  # noqa: BLE001 — 보고 실패가 요청 처리 결과를 덮으면 안 된다
            log.error("느린 요청 보고 실패 : %s", exc)
            return None

    def _report(self, meta: SlowRequestMeta) -> str | None:
        diagnosis = self._diagnostician.diagnose(meta.elapsed_wall, meta.mono_elapsed)
        breakdown = self._calculator.compute(meta.session_id, since_ts=meta.started)
        usage_rows = self._usage_row_builder.build(
            meta.usage, self._troubleshoot_channel, session_id=meta.session_id, model=meta.model,
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
    "UsageRowBuilder",
    "detect_retries",
    "tail_output",
]
