"""Analyzes channel history to extract learning proposals.

Each channel is analyzed separately rather than concatenated into one prompt —
a prior version truncated combined text at 40000 chars and silently dropped
later channels.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..engine.base import CallOrigin, EngineRequest
from ..engine.runner import EngineInvoker
from .decoder import ChannelAnalysisResult, ProposalDecoder
from .progress import ChannelFailure, FailureKind

# Re-exported: the result type moved to .decoder along with the shape checking
# that produces it, and existing imports read it from here.
__all__ = [
    "BuildResult", "ChannelAnalysis", "ChannelAnalysisResult",
    "ProposalAnalyzer", "ProposalBuilder",
]


@dataclass(frozen=True)
class ChannelAnalysis:
    """One channel's analysis: either a parsed result or a classified failure.

    A plain Outcome flattened the reason to a string, which lost the engine
    layer's already-structured usage_limit signal — and the batch needs that to
    tell "approve the switch and it works" from "this channel just fails"
    (sca-b4o).
    """

    result: ChannelAnalysisResult | None = None
    failure: ChannelFailure | None = None

    @property
    def ok(self) -> bool:
        return self.result is not None

    @classmethod
    def succeeded(cls, result: ChannelAnalysisResult) -> ChannelAnalysis:
        return cls(result=result)

    @classmethod
    def failed(cls, failure: ChannelFailure) -> ChannelAnalysis:
        return cls(failure=failure)


@dataclass(frozen=True)
class BuildResult:
    """What one analysis round produced, per channel."""

    results: Mapping[str, ChannelAnalysisResult]
    failures: tuple[ChannelFailure, ...]

_PROMPT_TEMPLATE = """아래는 슬랙봇 <<봇>>가 오늘 <<채널>> 채널에서 낸 응답과, 그 뒤에 사람이 남긴 말이다.

이 기록에서 <<봇>>가 앞으로 기억해야 할 것을 뽑아라.

뽑을 것
- 확정된 사실. 조회로 확인됐고 다시 물어봐도 같은 답이 나올 내용
- 사람이 지적한 형식 문제와 그 교정 방향
- 사람이 정정한 내용. <<봇>>가 틀렸고 사람이 바로잡은 것
- 사람이 말로 짚지 않았어도 기록에서 스스로 드러나는 동작 문제.
  같은 답을 중복으로 보낸 것, 부르지 말아야 할 사람을 불러 버린 것,
  하지 말아야 할 말을 한 것 같은, 대화 흐름을 보면 알 수 있는 실수

뽑지 말 것
- 추정과 가설
- 한 번 쓰고 끝날 일회성 값
- 이미 지식 파일에 있는 내용
- 사람의 개인 사정과 사적 대화

출력 형식. 아래 JSON 하나만 내라. 다른 문장을 붙이지 마라.

{
  "writing_style": ["형식 교정 항목. 없으면 빈 배열"],
  "channel_facts": ["이 채널에서 확정된 사실이나 자기 발견 시행착오. 없으면 빈 배열"],
  "corrections": ["<<봇>>가 틀렸던 것과 바른 내용"],
  "note": "제안이 없으면 그 이유를 한 문장으로"
}

각 항목은 한 문장으로 쓴다. 확정 날짜를 함께 적는다.
근거가 약하면 넣지 마라. 적게 뽑는 편이 낫다."""

_ARCHIVE_LIMIT = 60000
_REACTIONS_LIMIT = 15000


class ProposalAnalyzer:
    """Sends one channel's history to the engine and parses the proposal.

    Goes through EngineInvoker rather than EngineRunner so this path gets the
    same fallback routing as a normal request. Calling the runner directly
    skipped FallbackEngine.run() entirely, so a usage limit hit during the
    learning batch never switched engines or recorded state (sca-dyb.9).
    """

    def __init__(
        self,
        invoker: EngineInvoker,
        *,
        model: str | None,
        effort: str,
        workdir: Path,
        bot_name: str,
        decoder: ProposalDecoder | None = None,
    ) -> None:
        self._invoker = invoker
        self._model = model
        self._effort = effort
        self._workdir = workdir
        self._bot_name = bot_name
        self._decoder = decoder or ProposalDecoder()

    def analyze_channel(
        self,
        day: str,
        channel_name: str,
        archive_text: str,
        reactions: Sequence[Mapping[str, object]],
    ) -> ChannelAnalysis:
        system_prompt = _PROMPT_TEMPLATE.replace("<<채널>>", channel_name).replace(
            "<<봇>>", self._bot_name
        )
        body = self._build_body(day, channel_name, archive_text, reactions)
        request = EngineRequest(
            prompt=body,
            system_prompt=system_prompt,
            session_id=None,
            resume=False,
            model=self._model,
            effort=self._effort,
            workdir=self._workdir,
            allowed_tools=("Read",),
        )
        # Nobody is waiting on the nightly batch, so it must not spend the
        # fallback's recovery probe that an interactive request needs.
        response = self._invoker.invoke(request, CallOrigin.BACKGROUND)
        if not response.ok:
            kind = (FailureKind.USAGE_LIMIT if response.failure_reason == "usage_limit"
                    else FailureKind.ENGINE_FAILED)
            return ChannelAnalysis.failed(ChannelFailure(
                channel=channel_name, kind=kind,
                detail=response.failure_reason or "분석 실행에 실패했다",
            ))

        outcome = self._decoder.decode(response.body)
        if not outcome.is_found:
            return ChannelAnalysis.failed(ChannelFailure(
                channel=channel_name, kind=FailureKind.DECODE_FAILED, detail=outcome.reason,
            ))
        return ChannelAnalysis.succeeded(outcome.value())

    def _build_body(
        self,
        day: str,
        channel_name: str,
        archive_text: str,
        reactions: Sequence[Mapping[str, object]],
    ) -> str:
        body = (
            f"오늘 날짜 : {day}\n채널 : {channel_name}\n\n"
            f"=== {self._bot_name} 응답 기록 ===\n{archive_text[:_ARCHIVE_LIMIT]}\n\n"
        )
        body += "=== 사람이 남긴 반응 ===\n"
        if reactions:
            body += json.dumps(list(reactions), ensure_ascii=False, indent=2)[:_REACTIONS_LIMIT]
        else:
            body += "(없음)"
        return body


class ChannelAnalyzer(Protocol):
    """What ProposalBuilder needs. ProposalAnalyzer satisfies it."""

    def analyze_channel(
        self,
        day: str,
        channel_name: str,
        archive_text: str,
        reactions: Sequence[Mapping[str, object]],
    ) -> ChannelAnalysis: ...


class ProposalBuilder:
    """Runs one analysis round over the channels it is given.

    Returns per-channel results rather than a merged proposal: the batch has to
    know which channels finished, and flattening failures into the proposal's
    note left it unable to tell a failed day from an uneventful one (sca-b4o).
    """

    def __init__(self, analyzer: ChannelAnalyzer) -> None:
        self._analyzer = analyzer

    def build(
        self,
        day: str,
        channel_archives: Mapping[str, str],
        reactions_by_channel: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
    ) -> BuildResult:
        reactions_by_channel = reactions_by_channel or {}
        results: dict[str, ChannelAnalysisResult] = {}
        failures: list[ChannelFailure] = []

        for channel_name, archive_text in channel_archives.items():
            reactions = reactions_by_channel.get(channel_name, ())
            analysis = self._analyzer.analyze_channel(day, channel_name, archive_text, reactions)
            if analysis.result is not None:
                results[channel_name] = analysis.result
            elif analysis.failure is not None:
                failures.append(analysis.failure)

        return BuildResult(results=results, failures=tuple(failures))
