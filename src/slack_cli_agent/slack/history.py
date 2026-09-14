"""슬랙 기록 조회 — 빈 응답 재시도.

원본 bot.py 의 `slack_ts`, `read_history`,
`wait_history_slot` 을 그대로 옮겼다 (이식 분류 — 함수 본문은 수정하지 않는다).

슬랙이 `ok` 와 함께 빈 `messages` 를 돌려주는 경우가 있다. 2026-09-11 원본
실측에서 표본 3000개 중 956개(약 31퍼센트)가 소수 7자리 타임스탬프였고, 그
값을 `oldest` 로 보내면 슬랙이 오류 없이 빈 목록을 준다. 실제로 기록이 없는
채널과 구분되지 않으므로 여러 번 읽어 가른다.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.errors import HistoryUnavailable


class HistoryReader:
    """채널 기록을 읽되 빈 결과를 곧이곧대로 믿지 않는다."""

    def __init__(
        self,
        client: Any,
        settings: RuntimeSettings,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._settings = settings
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_call = 0.0

    def slack_ts(self, value: Any) -> str:
        """초 단위 시각을 슬랙이 받는 타임스탬프 문자열로 만든다.

        슬랙 타임스탬프는 소수 6자리다. `str(time.time())` 은 파이썬이 왕복
        가능한 가장 짧은 표기를 쓰기 때문에 소수 7자리가 나오는 경우가 있다.
        그 값을 oldest 로 보내면 슬랙이 오류 없이 빈 목록을 돌려준다.
        `ok` 가 참이라 호출자는 실제로 글이 없다고 읽는다.
        """
        return f"{float(value):.6f}"

    def wait_history_slot(self) -> None:
        """기록 조회를 너무 빨리 잇달아 부르지 않게 간격을 지킨다.

        빈 응답을 받고 재시도하는 것보다 애초에 빈 응답이 오지 않게 하는 편이 낫다.
        """
        with self._lock:
            gap = self._clock() - self._last_call
            min_interval = self._settings.history_min_interval_sec
            if gap < min_interval:
                self._sleep(min_interval - gap)
            self._last_call = self._clock()

    def read_history(self, channel: str, oldest: str, limit: int) -> list[dict[str, Any]]:
        """채널 기록을 읽되 빈 결과를 곧이곧대로 믿지 않는다.

        슬랙이 ok 를 주면서 messages 를 비워 보내는 일이 있다. 오류가 아니라
        정상 응답이라 그대로 쓰면 "놓친 요청이 없다" 가 되고, 실제로 기다리던
        사람은 답을 못 받는다.

        원본은 여러 번 읽어도 비면 `None` 을 돌려주지만, 이 포팅은
        `HistoryUnavailable` 을 낸다 — 실제로 비어 있는 것과 판정 불가를
        타입으로 구분한다. `core.errors.HistoryUnavailable` 의 정의가 이
        상황을 그대로 가리킨다.
        """
        empty = 0
        tries = self._settings.history_read_tries
        for attempt in range(tries):
            self.wait_history_slot()
            res = self._client.conversations_history(
                channel=channel, oldest=oldest, limit=limit
            )
            msgs = res.get("messages") or []
            if msgs:
                return msgs
            empty += 1
            if attempt < tries - 1:
                self._sleep(self._settings.history_read_pause_sec)
        raise HistoryUnavailable(
            f"{channel} 기록이 {empty}번 내리 비어 있다. 없다고 단정하지 않는다."
        )
