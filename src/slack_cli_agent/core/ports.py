"""요청 처리의 공용 계약.

워커는 큐를 소비하고, 파이프라인은 요청 하나를 끝까지 처리한다. 둘을 이
계약 하나로만 잇는다 — 워커가 파이프라인 구현을 직접 알면 큐 소비 로직
하나를 시험하는 데 엔진·슬랙·프롬프트까지 전부 대역으로 세워야 한다.

공유할 기본 구현이 없어 ABC 가 아니라 Protocol 이다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .context import RequestContext


@dataclass(frozen=True)
class HandleOutcome:
    """요청 하나를 처리한 결과. 워커가 큐 상태 전이에 쓴다."""

    ok: bool
    failure: str = ""
    """실패 사유. 성공이면 빈 문자열이다."""
    posted_ts: str = ""
    """올린 글의 슬랙 시각. 못 올렸거나 답하지 않았으면 빈 문자열이다."""
    silent: bool = False
    """답하지 않기로 판정한 것. 처리 실패가 아니라서 ok 는 True 로 둔다.

    실패와 나누는 이유는 리액션 표식이 다르기 때문이다. 침묵은 입다문 표식을
    달고 완료로 빠지고, 실패는 x 표식을 달고 재시도 대상이 된다.
    """


@runtime_checkable
class RequestHandler(Protocol):
    def handle(self, ctx: RequestContext) -> HandleOutcome:
        """요청 하나를 끝까지 처리한다.

        예외를 밖으로 내지 않는다. 처리 중 어떤 실패가 나든 `ok=False` 와
        사유를 담아 돌려준다 — 워커가 예외 처리를 대신하면 그 사유가 큐에
        남지 않는다.
        """
