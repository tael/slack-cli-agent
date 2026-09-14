"""슬랙 메시지를 무엇으로 볼지 판정한다.

`subtype` 과 `bot_id` 로 메시지를 거르는 코드가 네 곳에 따로 있었고 기준이
갈렸다. 소켓 이벤트를 받는 쪽은 파일을 붙여 보낸 말을 사람이 새로 건넨 말로
받는데, 되짚기·추가 발언 수집·대화 기록은 `subtype` 이 있으면 무조건 걸렀다.
그래서 파일을 붙여 부른 요청은 소켓으로 들어올 때만 처리되고 재기동 중에
들어오면 유실된다.

판정을 여기 하나로 둔다. 받아들이는 `subtype` 목록을 바꿀 때 고치는 위치도
하나다.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# 파일을 붙여 보낸 말은 subtype 이 있어도 사람이 새로 건넨 말로 친다.
HUMAN_SUBTYPES = frozenset({"file_share"})


class MessageKind:
    """메시지 하나를 보고 그것이 무엇인지 판정한다."""

    def __init__(self, human_subtypes: frozenset[str] = HUMAN_SUBTYPES) -> None:
        self._human_subtypes = human_subtypes

    def is_human(self, msg: Mapping[str, Any]) -> bool:
        """사람이 새로 건넨 말인가.

        봇이 올린 것은 제외한다. `subtype` 이 붙은 것도 제외하되, 파일 첨부처럼
        사람의 발언에 붙는 것은 받아들인다.
        """
        if msg.get("bot_id"):
            return False
        subtype = msg.get("subtype")
        return not subtype or subtype in self._human_subtypes

    def is_transcribable(self, msg: Mapping[str, Any]) -> bool:
        """대화 기록에 넣을 말인가.

        봇의 답도 넣는다. 사람 말만 남기면 무엇에 대한 답인지가 끊긴다.
        """
        return bool(msg.get("bot_id")) or self.is_human(msg)
