"""관리 명령의 계약.

원본은 슬랙 메시지로 오는 관리 명령을 `handle_admin` 안의 if-elif 사슬
하나로 처리한다(01-source-analysis.md 18절). 명령마다 클래스로 나누고
권한 판정은 명령이 아니라 `AdminRouter` 가 한다 — 명령마다 권한 검사를
반복해 적으면 하나를 빠뜨리는 사고가 난다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from ..auth.principal import Principal, TrustLevel

if TYPE_CHECKING:
    from ..config.channel import ChannelRegistry
    from ..config.profile import Profile


@dataclass(frozen=True)
class AdminContext:
    """관리 명령 실행 하나의 입력."""

    principal: Principal
    channel: str
    thread_ts: str
    channels: ChannelRegistry
    profile: Profile
    text: str = ""
    """받은 본문 그대로. 라우터가 채운다.

    대부분의 명령은 `matches` 로 판정이 끝나 본문을 다시 안 본다. 되돌리기처럼
    본문에 인자가 붙는 명령만 이 값을 읽는다. 기본값이 빈 문자열이라 맥락을
    만드는 쪽은 그대로 둬도 된다.
    """


@dataclass(frozen=True)
class AdminResult:
    """관리 명령 실행 결과. 사용자에게 그대로 보일 문구다."""

    message: str
    handled: bool = True
    """False 면 명령은 찾았으나 권한이 모자라 실행하지 않았다는 뜻이다."""


class AdminCommand(ABC):
    """관리 명령 하나의 계약."""

    name: ClassVar[str]
    required_trust: ClassVar[TrustLevel] = TrustLevel.OWNER
    """원본은 관리 명령 전부를 `user != OWNER_USER_ID` 하나로 막는다.
    기본값을 OWNER 로 두어 그 동작을 그대로 잇는다."""

    @abstractmethod
    def matches(self, text: str) -> bool:
        """이 본문이 이 명령인가."""

    @abstractmethod
    def execute(self, ctx: AdminContext) -> AdminResult:
        """명령을 실행한다. 권한 판정은 라우터가 이미 마쳤다고 가정한다."""
