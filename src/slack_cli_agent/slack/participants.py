"""스레드에 함께 있는 사람을 추린다.

원본 `bot.py` 의 `recent_thread()` 와 `thread_people()` 을 합쳐 옮겼다.
말한 사람과 멘션으로 불려 들어온 사람이 대상이다 — 멘션은 알림이 가고
스레드가 열려 있으므로 그 자리에 있는 것으로 본다.

본문에서 누가 함께 있는지를 추론하게 두면 없는 사람 취급을 하는 답이
나온다. 세어서 프롬프트에 적어 준다.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Protocol

# 슬랙 본문의 멘션 표기. `<@U123>` 또는 `<@U123|이름>` 둘 다 받는다.
MENTION_IN_TEXT = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")

UNKNOWN_NAME = "이름 모르는 사람"


class ThreadHistory(Protocol):
    def read_thread(self, channel: str, thread_ts: str, limit: int) -> list[Any]: ...


class ThreadParticipants:
    """스레드 메시지를 읽어 참여자 목록을 만든다."""

    def __init__(
        self,
        history: ThreadHistory,
        name_resolver: Callable[[str], str],
        bot_user_id: str,
        limit: int = 40,
    ) -> None:
        self._history = history
        self._name_resolver = name_resolver
        self._bot_user_id = bot_user_id
        self._limit = limit

    def of(self, channel: str, thread_ts: str) -> tuple[tuple[str, str], ...]:
        """(표시 이름, 멘션 표기) 의 목록. 처음 나온 순서를 지킨다."""
        # 디엠에는 봇과 상대뿐이라 셀 것이 없다. 원본 `recent_thread()` 와 같다.
        if channel.startswith("D"):
            return ()
        try:
            messages = self._history.read_thread(channel, thread_ts, self._limit)
        except Exception:  # noqa: BLE001 — 참가자 조회 실패로 요청 자체를 실패시키지 않는다 — 프롬프트에 그 대목만 빠진다
            # 함께 있는 사람을 못 셌다고 요청 자체를 실패시키지 않는다.
            # 이 값이 없으면 프롬프트에 그 대목이 안 붙을 뿐이다.
            return ()

        seen: set[str] = set()
        people: list[tuple[str, str]] = []

        def add(user_id: str | None) -> None:
            if not user_id or user_id in seen or user_id == self._bot_user_id:
                return
            seen.add(user_id)
            people.append((self._name_resolver(user_id) or UNKNOWN_NAME, f"<@{user_id}>"))

        for message in messages:
            # 봇이 보낸 말의 발신자는 사람이 아니다. 그 안의 멘션은 그대로 센다.
            if not message.get("bot_id"):
                add(message.get("user"))
            for user_id in MENTION_IN_TEXT.findall(message.get("text") or ""):
                add(user_id)
        return tuple(people)
