# Notice text is centralized here so `is_notice()` has a single source of
# truth -- if a notice were counted as a real reply, catch-up would treat
# the earlier request as answered and it would get lost permanently.

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any


class NoticeKey(str, Enum):
    LATE = "late"
    BUSY = "busy"
    FULL = "full"
    JOINED = "joined"
    RESTART = "restart"
    ASK_WHAT = "ask_what"
    NOT_LISTED = "not_listed"
    MODE_MENTION_ONLY = "mode_mention_only"
    MODE_STRUCTURED = "mode_structured"
    MODE_PLAIN = "mode_plain"
    CHAT_ACTIVE = "chat_active"
    CHAT_NORMAL = "chat_normal"
    CHAT_QUIET = "chat_quiet"
    CHANNEL_UNREGISTERED = "channel_unregistered"
    UNADDRESSED_ON = "unaddressed_on"
    UNADDRESSED_OFF = "unaddressed_off"


DEFAULT_NOTICES: Mapping[NoticeKey, str] = {
    NoticeKey.LATE: "답이 늦었어요. 지금 확인해서 이어서 답할게요.",
    NoticeKey.BUSY: "앞 요청을 처리 중입니다. 끝나면 이어서 답하겠습니다.",
    NoticeKey.FULL: "처리 대기가 한도를 넘었습니다. 잠시 후 다시 멘션해주세요.",
    NoticeKey.JOINED: "이 채널에 등록했습니다. 이제 여기서 답합니다.",
    NoticeKey.RESTART: "지금은 재시작 중이에요. 잠시 후 다시 불러주세요.",
    NoticeKey.ASK_WHAT: "무엇을 확인할까요.",
    NoticeKey.NOT_LISTED: "이 채널은 목록에 없습니다.",
    NoticeKey.MODE_MENTION_ONLY: "멘션 전용 모드로 바꿨어요. 이제 이름을 불러야 답합니다.",
    NoticeKey.MODE_STRUCTURED: "지정된 응답 형식으로 바꿨습니다.",
    NoticeKey.MODE_PLAIN: "일반 응답 형식으로 되돌렸습니다.",
    NoticeKey.CHAT_ACTIVE: "말수를 늘렸어요.",
    NoticeKey.CHAT_NORMAL: "말수를 기본으로 되돌렸어요.",
    NoticeKey.CHAT_QUIET: "용건 있을 때만 나설게요.",
    NoticeKey.CHANNEL_UNREGISTERED: "{name} 을 목록에서 뺐습니다.\n다시 부르시면 등록됩니다.",
    NoticeKey.UNADDRESSED_ON: "이 채널에서는 이름을 안 불러도 답합니다.\n제가 이미 낀 스레드에서만 나섭니다.",
    NoticeKey.UNADDRESSED_OFF: "이제 이름을 불러야 답합니다.",
}


class NoticeCatalog:
    def __init__(self, overrides: Mapping[Any, str] | None = None) -> None:
        self._texts: dict[Any, str] = dict(DEFAULT_NOTICES)
        if overrides:
            self._texts.update(overrides)

    def render(self, key: Any, **params: Any) -> str:
        text = self._texts[key]
        return text.format(**params) if params else text

    def is_notice(self, text: str | None) -> bool:
        stripped = (text or "").strip()
        if not stripped:
            return False
        return stripped in self._texts.values()
