"""Permission layer bundling what were separate judgment functions under a
single `Principal` input.

Company-specific rules aren't here — only the `AccessExtension` extension
point; plugins implement the actual logic.
"""

from __future__ import annotations

from abc import ABC
from collections.abc import Sequence
from typing import TYPE_CHECKING

from ..core.channel_kind import is_direct_message_channel
from .principal import Principal, TrustLevel

if TYPE_CHECKING:
    from ..config.channel import ChannelRegistry
    from ..config.profile import Profile

# Lower means shallower thinking. A channel's effort value is a ceiling for
# other users, not a floor for the owner — an owner request never drops
# below OWNER_EFFORT_MIN.
EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
OWNER_EFFORT_MIN = "medium"
DEFAULT_EFFORT = "medium"


class AccessExtension(ABC):  # noqa: B024 — 확장점이다. 기본이 no-op 이라 강제할 메서드가 없다
    """Extension point for company-specific permission logic. No-op by
    default, so not extending changes nothing."""

    def applies(self, principal: Principal, prompt: str = "") -> bool:
        return False

    def extra_tools(self, principal: Principal) -> Sequence[str]:
        return ()


class AccessPolicy:
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
        """Owner is OWNER regardless of channel. Everyone else is TRUSTED
        only if the channel's trusted_users lists them."""
        is_dm = is_direct_message_channel(channel)
        if user and user == self._profile.owner_user_id:
            trust = TrustLevel.OWNER
        elif user and self._is_channel_trusted(channel, user):
            trust = TrustLevel.TRUSTED
        else:
            trust = TrustLevel.GENERAL
        return Principal(user_id=user, channel=channel, trust=trust, is_direct_message=is_dm)

    def accepts_request(self, principal: Principal) -> bool:
        """Whether this conversation gets an answer at all (bot.py:3695).

        The owner passes anywhere. Any other DM is refused, and a channel
        passes only if it is registered. An empty owner_user_id makes nobody
        the owner, which is stricter than the original -- there `user` and
        `OWNER_USER_ID` both being empty would have passed.
        """
        if principal.trust is TrustLevel.OWNER:
            return True
        if principal.is_direct_message:
            return False
        return self._channels.is_registered(principal.channel)

    def may_disclose_mechanism(self, principal: Principal) -> bool:
        """True in the owner's own DM, when the channel's
        disclose_mechanism is on, or when an extension allows it."""
        if self.is_full_authority(principal):
            return True
        if self._channel_flag(principal.channel, "disclose_mechanism"):
            return True
        return any(ext.applies(principal) for ext in self._extensions)

    def may_see_usage(self, principal: Principal) -> bool:
        """Token usage can only be mentioned in the owner's own DM, even if
        the owner asks elsewhere."""
        return self.is_full_authority(principal)

    def model_for(self, principal: Principal) -> str:
        """Model to use. The owner always gets the owner model regardless of channel."""
        if principal.trust is TrustLevel.OWNER:
            return self._profile.primary_engine.model_for_owner()
        config = self._channels.get(principal.channel)
        if config and config.model:
            return config.model
        return self._profile.primary_engine.model

    def effort_for(self, principal: Principal, prompt: str) -> str:
        """'ultrathink' in the prompt bumps effort to high. Otherwise uses
        the channel's value, except the owner never drops below
        OWNER_EFFORT_MIN — a channel's setting caps other users but never
        lowers the owner's.
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
        """True only in the owner's own DM."""
        return principal.trust is TrustLevel.OWNER and principal.is_direct_message

    def channel_tools_for(self, principal: Principal) -> tuple[str, ...]:
        """Extra tools this channel grants this user. The owner is left out —
        owner tools already cover it, same as the original (bot.py:110)."""
        if principal.trust is TrustLevel.OWNER:
            return ()
        config = self._channels.get(principal.channel)
        if not config:
            return ()
        return tuple(config.user_tools.get(principal.user_id, ()))

    def _is_channel_trusted(self, channel: str, user: str) -> bool:
        config = self._channels.get(channel)
        return bool(config and user in config.trusted_users)

    def _channel_flag(self, channel: str, key: str) -> bool:
        """Reads a boolean channel setting; missing config reads as off.

        Only reads declared fields, not `extra` — a typo there wouldn't be
        caught by type checking.
        """
        config = self._channels.get(channel)
        if not config:
            return False
        return bool(getattr(config, key))
