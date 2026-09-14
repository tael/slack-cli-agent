"""요청자의 신원. 채널과 신뢰 등급을 하나로 묶는다.

원본은 소유자 여부와 채널 성격을 여러 함수가 각자 다른 인자로 따로 받았다.
여기서는 그 판정 결과를 값 객체 하나로 고정한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class TrustLevel(IntEnum):
    """신뢰 등급. 값이 클수록 넓은 권한이다.

    GENERAL   일반 사용자. 채널이 정한 규칙만 적용된다
    TRUSTED   채널 설정의 trusted_users 에 등록된 사용자
    OWNER     프로필의 owner_user_id 와 일치하는 사용자. 채널과 무관하게
              모델·effort 상향이 적용된다. 원본의 is_owner 와 같은 축이다
    """

    GENERAL = 0
    TRUSTED = 1
    OWNER = 2


@dataclass(frozen=True)
class Principal:
    """요청 하나를 보낸 사람의 신원과 그 채널의 성격."""

    user_id: str
    channel: str
    trust: TrustLevel
    is_direct_message: bool
