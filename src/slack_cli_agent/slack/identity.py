"""이 봇 자신의 슬랙 신원 — 자기 말과 다른 봇의 말을 가르는 단일 근거.

같은 채널에서 다른 슬랙 봇이 함께 답한다. `bot_id` 가 있다는 것만으로 이 봇의
말로 보면 다른 봇의 답까지 이 봇의 답으로 세어지고, 되짚기가 실제 미응답 멘션을
복구 대상에서 뺀다. 원본 `bot.py` 의 `is_self()` 가 2026-09-02 에 그 사고로
고쳐진 부분이다.

**판정을 한 클래스로 모은 이유가 있다.** 재구성 과정에서 같은 `_is_self` 가
`Application`·`TranscriptBuilder`·`EventListener` 에 각각 구현됐고, 조립 코드가
그중 하나에만 `bot_id` 를 넘겼다. 나머지 둘은 생성자 기본값이 빈 문자열이라 그
사실이 드러나지 않은 채 예전 판정으로 돌아갔고, 부품 시험은 전부 통과했다.
근거를 하나로 두고 그것을 주입받게 하면 그 형태가 다시 생기지 않는다.
"""

from __future__ import annotations

import logging
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_RETRY_INTERVAL_SEC = 60.0


class BotIdentity(ABC):
    """이 봇의 신원을 알려주는 계약. 쓰는 쪽은 조회 방식을 모른다."""

    @property
    @abstractmethod
    def user_id(self) -> str:
        """이 봇의 슬랙 사용자 ID. 모르면 빈 문자열이다."""

    @property
    @abstractmethod
    def bot_id(self) -> str:
        """이 봇의 `bot_id`. 모르면 빈 문자열이다."""

    @property
    @abstractmethod
    def known(self) -> bool:
        """판정에 쓸 신원을 확보했는가."""

    @abstractmethod
    def is_self(self, msg: Mapping[str, Any]) -> bool:
        """그 메시지를 이 봇이 올렸는가."""

    @abstractmethod
    def is_mentioned(self, text: str) -> bool:
        """그 본문이 이 봇을 부르는가."""


class SlackBotIdentity(BotIdentity):
    """`auth_test` 로 신원을 받아 판정한다.

    조회는 처음 필요할 때 한 번 한다. 판정마다 부르면 요청 수만큼 API 호출이
    늘어난다. 성공하면 그 값을 계속 쓴다.

    실패는 캐시하지 않는다. 한 번 실패했다고 포기하면 슬랙 API 의 일시 장애가
    그 프로세스가 사는 내내 이어지는 오판이 된다. 다만 판정마다 다시 부르면
    장애가 이어지는 동안 호출이 폭증하므로 재조회에 간격을 둔다.
    """

    def __init__(
        self,
        client: Any,
        *,
        clock: Callable[[], float] = time.monotonic,
        retry_interval_sec: float = DEFAULT_RETRY_INTERVAL_SEC,
    ) -> None:
        self._client = client
        self._clock = clock
        self._retry_interval_sec = retry_interval_sec
        self._user_id = ""
        self._bot_id = ""
        self._attempted_at: float | None = None

    @property
    def user_id(self) -> str:
        self._load()
        return self._user_id

    @property
    def bot_id(self) -> str:
        self._load()
        return self._bot_id

    @property
    def known(self) -> bool:
        self._load()
        return self._has_identity()

    def is_self(self, msg: Mapping[str, Any]) -> bool:
        """그 메시지를 이 봇이 올렸는가.

        신원을 아직 못 받았으면 False 다. 원본은 이 경우
        `bool(msg.get("bot_id"))` 로 돌아가는데 그것이 사고를 낸 예전 방식
        그대로다. 오판의 두 방향 중 방어가 있는 쪽으로 기운다 — 이 봇의 답을
        남의 것으로 보면 되짚기가 재등록을 시도해도 jobs 표의
        `(channel, message_ts)` 유일 제약이 중복을 막지만, 반대 방향은 막는
        것이 없어 미응답 멘션이 그대로 유실된다.
        """
        if not self.known:
            return False
        if self._bot_id and msg.get("bot_id"):
            return bool(msg.get("bot_id") == self._bot_id)
        if self._user_id and msg.get("user"):
            return bool(msg.get("user") == self._user_id)
        return False

    def is_mentioned(self, text: str) -> bool:
        """그 본문이 이 봇을 부르는가.

        슬랙은 표시 이름을 붙여 `<@U123|이름>` 형태로 보내기도 한다. 문자열
        포함으로 보면 그 형태를 못 잡고, 반대로 `<@U_BOT2>` 안의 `U_BOT` 에
        걸리기도 한다. 두 경우 모두 되짚기가 부른 말을 잘못 판정한다.

        신원을 아직 못 받았으면 False 다. `is_self` 와 같은 이유로 방어가 있는
        쪽으로 기운다.
        """
        user_id = self.user_id
        if not user_id:
            return False
        return bool(re.search(rf"<@{re.escape(user_id)}(?:\|[^>]*)?>", text))

    def _has_identity(self) -> bool:
        """조회를 유발하지 않고 지금 값만 본다.

        조회가 성공해도 값이 비어 있으면 확보한 것이 아니다. 빈 값으로는 어떤
        메시지도 대조할 수 없다.
        """
        return bool(self._bot_id or self._user_id)

    def _load(self) -> None:
        if self._has_identity():
            return
        now = self._clock()
        if self._attempted_at is not None and now - self._attempted_at < self._retry_interval_sec:
            return
        self._attempted_at = now
        try:
            info = self._client.auth_test() or {}
        except Exception:  # noqa: BLE001 — 조회 실패로 여기서 죽으면 이후 처리 전체가 막힌다
            log.warning("봇 신원을 조회하지 못했다")
            info = {}
        self._user_id = str(info.get("user_id") or "")
        self._bot_id = str(info.get("bot_id") or "")
