"""SpeakerNamer — 슬랙 메시지의 화자 표시를 한 곳에서 만든다.

`TranscriptBuilder._speaker_of` 와 `LateAddendumChecker._speaker_of` 가 각각
같은 로직을 따로 들고 있었다. 사용자에게 보이는 화자 표시가 두 곳에서 따로
만들어지면, 한쪽에 이름 규칙을 더해도 다른 쪽 화면은 옛 표기로 남는다. 봇
분기를 포함한 완전한 판정을 여기 하나로 모으고 두 사용처는 이 클래스에
위임만 한다.

`late_addendum` 쪽은 이 봇 메시지든 다른 봇 메시지든 `MessageKind.is_human`
이 미리 걸러 내서 `speaker_of` 에 넘기지 않는다. 그래서 `is_self` 자리에
언제나 False 를 돌려주는 함수를 넘겨도 지금 화면은 그대로다 — 봇 분기가 있는
코드를 쓰지만 그 분기를 탈 메시지가 안 들어온다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


class SpeakerNamer:
    """메시지 하나를 보고 그 화자를 사람이 읽을 표시 문자열로 만든다."""

    def __init__(
        self,
        name_resolver: Callable[[str], str],
        is_self: Callable[[Mapping[str, Any]], bool],
        bot_display_name: str,
        owner_user_id: str,
        owner_display_name: str,
    ) -> None:
        self._name_resolver = name_resolver
        self._is_self = is_self
        self._bot_display_name = bot_display_name
        self._owner_user_id = owner_user_id
        self._owner_display_name = owner_display_name

    def speaker_of(self, msg: Mapping[str, Any]) -> str:
        """그 메시지를 누가 보냈는지 표시 문자열로 돌려준다."""
        if self._is_self(msg):
            return self._bot_display_name or "봇"
        if msg.get("bot_id"):
            profile = msg.get("bot_profile") or {}
            name = profile.get("name") or msg.get("username") or ""
            return f"{name} (다른 봇)" if name else "이름 모르는 봇"
        user = msg.get("user") or ""
        if user == self._owner_user_id and self._owner_display_name:
            return self._owner_display_name
        name = self._name_resolver(user) or "이름 모르는 사람"
        return f"{name} <@{user}>" if user else name
