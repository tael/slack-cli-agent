"""권한 계층. 원본의 판정 함수 4개를 하나로 모은다.

원본은 is_trusted, full_authority, mechanism_open, 조직 전용 권한 판정 이 서로
다른 인자를 받고 서로를 모른 채 흩어져 있었다. 여기서는 Principal 하나를
공통 입력으로 받는 메서드로 합친다.

회사 전용 조건(원본의 조직 전용 권한 판정)은 여기 넣지 않는다. AccessExtension
확장점만 두고 구현은 플러그인이 담당한다.
"""

from __future__ import annotations

from abc import ABC
from collections.abc import Sequence
from typing import TYPE_CHECKING

from .principal import Principal, TrustLevel

if TYPE_CHECKING:
    from ..config.channel import ChannelRegistry
    from ..config.profile import Profile

# 원본 EFFORT_LEVELS, OWNER_EFFORT_MIN 그대로.
# 낮을수록 얕게 생각한다. 채널값은 다른 사람을 위한 상한이지 소유자의 하한이
# 아니다 — 소유자 요청은 이 아래로 내려가지 않는다.
EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
OWNER_EFFORT_MIN = "medium"
DEFAULT_EFFORT = "medium"


class AccessExtension(ABC):
    """회사 전용 판정을 붙이는 확장점.

    기본 구현은 아무 효과가 없다. 확장하지 않으면 권한도 도구도 늘지 않는다.
    원본의 조직 전용 권한 판정 이 이 계약을 구현하는 플러그인으로 옮겨간다.
    """

    def applies(self, principal: Principal, prompt: str = "") -> bool:
        return False

    def extra_tools(self, principal: Principal) -> Sequence[str]:
        return ()


class AccessPolicy:
    """요청 하나의 권한을 판정한다."""

    def __init__(
        self,
        profile: Profile,
        channels: ChannelRegistry,
        extensions: Sequence[AccessExtension] = (),
    ) -> None:
        self._profile = profile
        self._channels = channels
        self._extensions = tuple(extensions)

    def principal_for(self, channel: str, user: str) -> Principal:
        """이 채널·이 사용자의 신원을 판정한다.

        소유자는 채널과 무관하게 OWNER 다 — 원본의 is_owner 가 채널을 안
        따지는 것과 같다. 다른 사용자는 그 채널의 trusted_users 목록에
        있을 때만 TRUSTED 다.
        """
        is_dm = channel.startswith("D")
        if user and user == self._profile.owner_user_id:
            trust = TrustLevel.OWNER
        elif user and self._is_channel_trusted(channel, user):
            trust = TrustLevel.TRUSTED
        else:
            trust = TrustLevel.GENERAL
        return Principal(user_id=user, channel=channel, trust=trust, is_direct_message=is_dm)

    def may_disclose_mechanism(self, principal: Principal) -> bool:
        """구조를 설명해도 되는 자리인가.

        소유자의 DM(원본의 full_authority)이거나, 채널 설정의
        disclose_mechanism 이 켜져 있으면(원본의 mechanism_open) 참이다.
        확장이 붙였으면 그것도 더한다.
        """
        if self.is_full_authority(principal):
            return True
        if self._channel_flag(principal.channel, "disclose_mechanism"):
            return True
        return any(ext.applies(principal) for ext in self._extensions)

    def may_see_usage(self, principal: Principal) -> bool:
        """토큰 사용 현황을 답에 써도 되는가.

        원본은 이 정보를 소유자의 DM 에서만 다룬다. 다른 자리에서는
        소유자가 물어도 답하지 않는다.
        """
        return self.is_full_authority(principal)

    def model_for(self, principal: Principal) -> str:
        """이 요청에 쓸 모델. 소유자는 채널과 무관하게 소유자용 모델이다."""
        if principal.trust is TrustLevel.OWNER:
            return self._profile.primary_engine.model_for_owner()
        config = self._channels.get(principal.channel)
        if config and config.model:
            return config.model
        return self._profile.primary_engine.model

    def effort_for(self, principal: Principal, prompt: str) -> str:
        """이 요청의 생각 깊이.

        본문에 ultrathink 가 있으면 high 로 올린다. 그 외에는 채널값을
        쓰되, 소유자는 OWNER_EFFORT_MIN 아래로 내려가지 않는다. 채널값은
        다른 사람을 위한 상한이지 소유자의 하한이 아니다.
        """
        if prompt and "ultrathink" in prompt.lower():
            return "high"
        config = self._channels.get(principal.channel)
        level = config.effort if (config and config.effort in EFFORT_LEVELS) else DEFAULT_EFFORT
        if principal.trust is TrustLevel.OWNER and (
            EFFORT_LEVELS.index(level) < EFFORT_LEVELS.index(OWNER_EFFORT_MIN)
        ):
            return OWNER_EFFORT_MIN
        return level

    def is_full_authority(self, principal: Principal) -> bool:
        """원본의 full_authority. 소유자의 DM 에서만 참이다."""
        return principal.trust is TrustLevel.OWNER and principal.is_direct_message

    def is_trusted_context(self, principal: Principal) -> bool:
        """원본의 is_trusted 와 대응. 소유자의 DM 이거나 채널이 신뢰를 준 자리다."""
        return self.is_full_authority(principal) or principal.trust is TrustLevel.TRUSTED

    def _is_channel_trusted(self, channel: str, user: str) -> bool:
        config = self._channels.get(channel)
        return bool(config and user in config.trusted_users)

    def _channel_flag(self, channel: str, key: str) -> bool:
        """채널 설정의 불린 항목을 읽는다. 채널 설정이 없으면 끈 것으로 본다.

        정식 필드만 읽는다. extra 로 조회하면 오타를 타입 검사로 검출하지 못한다.
        """
        config = self._channels.get(channel)
        if not config:
            return False
        return bool(getattr(config, key))
