"""상태 안내문 목록.

봇이 내는 상태 메시지(처리 중, 한도 소진, 오류 등)를 목록으로 관리한다.
안내문이 답변으로 세어지면 되짚기가 그 요청을 처리된 것으로 짝지어 앞 요청이
영영 묻힌다. 그래서 리터럴을 흩어 두지 않고 상수로 모아, 안내문인지 여부를
한 곳에서 판정한다.

문구는 설정으로 덮어쓸 수 있게 하되 기본값은 코드에 둔다 — 설정이 없어도
동작해야 하기 때문이다.
"""

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


# 원본 NOTICE_* 상수를 옮긴 것이다. 조직 고유 표현(제품명, 조직 전용 모드
# 이름, 조직 내부 문의처)은 일반화했다 — MODE_MENTION_ONLY 와
# MODE_STRUCTURED 가 그 대응이다.
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
}


class NoticeCatalog:
    def __init__(self, overrides: Mapping[Any, str] | None = None) -> None:
        self._texts: dict[Any, str] = dict(DEFAULT_NOTICES)
        if overrides:
            self._texts.update(overrides)

    def render(self, key: Any, **params: Any) -> str:
        """등록된 안내문을 돌려준다. 없는 키면 KeyError."""
        text = self._texts[key]
        return text.format(**params) if params else text

    def is_notice(self, text: str | None) -> bool:
        """이 문자열이 안내문인지 판정한다. 답변 여부 판정(되짚기)이 이것을 쓴다."""
        stripped = (text or "").strip()
        if not stripped:
            return False
        return stripped in self._texts.values()
