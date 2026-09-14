"""신뢰성 계층이 슬랙 기록 조회에 기대는 계약.

`slack/history.py` 는 다른 작업자가 동시에 만들고 있어 이 파일을 쓰는 시점에는
없다. `CatchupService` 는 이 Protocol 에만 의존하고, 시험은 대역으로 한다.
통합 시점에 실물 구현으로 바꾼다.

타임스탬프 정밀도(소수 6자리)와 빈 응답 재시도는 이 계약의 구현이 책임진다.
원본 `slack_ts`, `read_history`, `wait_history_slot` 이 그 자리다 — 호출부인
`CatchupService` 는 초 단위 실수만 다루고 문자열 변환을 하지 않는다.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class HistoryReader(Protocol):
    def read_history(
        self, channel: str, oldest: float, limit: int
    ) -> list[Mapping[str, Any]] | None:
        """`channel` 의 최상위 메시지를 `oldest`(초 단위 실수) 이후로 최대
        `limit` 건 읽는다.

        슬랙이 `ok` 를 주면서 빈 목록을 돌려주는 경우가 있다 — 오류가 아니라
        정상 응답이라 그대로 믿으면 "놓친 요청이 없다" 가 된다. 구현은 여러 번
        읽어 빈 응답인지 실제로 없는 것인지 가른다.

        돌려주는 값
          목록 : 믿을 수 있는 결과. 실제로 비어 있으면 빈 목록이다.
          None : 여러 번 읽어도 비어 판정 불가. "없다" 로 해석하면 안 된다.
        """

    def read_thread(
        self, channel: str, thread_ts: str, limit: int
    ) -> list[Mapping[str, Any]]:
        """그 스레드의 메시지 전체(부모 포함)를 시간순으로 읽는다.

        조회에 실패하면 빈 목록을 돌려준다. 스레드 하나를 못 읽었다고 전체
        되짚기를 판정 불가로 만들 필요는 없다 — 호출부가 그 스레드만 건너뛴다.
        """
