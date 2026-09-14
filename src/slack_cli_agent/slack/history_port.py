"""신뢰성 계층의 `HistoryReader` Protocol 과 슬랙 실물 구현을 잇는 어댑터.

`reliability.ports.HistoryReader` 와 `slack.history.HistoryReader` 는 시그니처가
다르다 — 판정 불가를 표현하는 방식(`None` 대 예외), `read_history` 의 `oldest`
타입(초 단위 실수 대 슬랙 타임스탬프 문자열), `read_thread` 의 존재 여부가 다르다.
이 클래스가 그 차이를 메운다. 실물의 로직(빈 응답 재시도, 소수 6자리 변환)은
고치지 않고 그대로 감싼다.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from slack_cli_agent.core.errors import HistoryUnavailable
from slack_cli_agent.slack.history import HistoryReader as RealHistoryReader


class SlackHistoryPort:
    """`reliability.ports.HistoryReader` Protocol 을 만족하는 어댑터."""

    def __init__(self, reader: RealHistoryReader, client: Any) -> None:
        self._reader = reader
        self._client = client

    def read_history(
        self, channel: str, oldest: float, limit: int
    ) -> list[Mapping[str, Any]] | None:
        """실물에 `oldest` 를 슬랙 타임스탬프 문자열로 변환해 넘긴다.

        실물이 여러 번 읽어도 빈 결과면 `HistoryUnavailable` 을 내는데, 이
        Protocol 은 판정 불가를 예외가 아니라 `None` 으로 표현한다. 여기서
        그 변환을 흡수한다 — 실제로 비어 있는 것(빈 목록)과는 구분된다.
        """
        oldest_str = self._reader.slack_ts(oldest)
        try:
            result = self._reader.read_history(channel, oldest_str, limit)
        except HistoryUnavailable:
            return None
        # list[dict] 는 list[Mapping] 의 하위형이 아니다 — list 는 원소 형에
        # 불변(invariant)이다. 원소는 실제로 dict 라 Mapping 을 만족하므로
        # 이 자리에서만 그 사실을 명시한다.
        return cast("list[Mapping[str, Any]]", result)

    def read_thread(
        self, channel: str, thread_ts: str, limit: int
    ) -> list[Mapping[str, Any]]:
        """스레드 메시지 전체를 읽는다. 실패하면 빈 목록을 돌려준다.

        스레드 하나를 못 읽었다고 되짚기 전체를 판정 불가로 만들 필요는
        없으므로 어떤 예외든 여기서 삼킨다. 호출 전 `wait_history_slot` 으로
        기록 조회 간격을 지킨다 — 스레드 조회도 같은 API 호출 제한을 쓴다.
        """
        self._reader.wait_history_slot()
        try:
            res = self._client.conversations_replies(
                channel=channel, ts=thread_ts, limit=limit
            )
            return res.get("messages") or []
        except Exception:  # noqa: BLE001 — 스레드 하나를 못 읽었다고 되짚기 전체를 판정 불가로 만들지 않는다
            return []
