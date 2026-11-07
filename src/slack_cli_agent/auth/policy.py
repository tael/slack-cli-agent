"""Permission layer bundling what were separate judgment functions under a
single `Principal` input.

Company-specific rules aren't here — only the `AccessExtension` extension
point; plugins implement the actual logic.
"""

from __future__ import annotations

from abc import ABC
from collections.abc import Sequence
from typing import TYPE_CHECKING

from ..config.channel import is_channel_operator
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

#: Bare tool names a channel's `operator_full` flag gates. A scoped grant like
#: `Bash(ops_ctl.sh:*)` is a different string and is never touched here -- only
#: the unrestricted name needs the flag, the same way mametchi's bot.py only
#: handed back bare `Bash` to an operator_full channel's operator (bot.py:
#: 2297-2299) while a scoped allowlist entry was never the thing that bug was
#: about. engine/claude.py reads this same set to decide whether the
#: settings-layer deny must step aside for a name this method already let
#: through -- one flag, one set of names, both axes.
OPERATOR_TOOLS: frozenset[str] = frozenset({"Bash", "Edit", "Write", "NotebookEdit"})

#: operator_level results. Empty means not an operator.
OPERATOR = "operator"
OPERATOR_FULL = "operator_full"


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
        """Owner and controller_user_ids are OWNER regardless of channel.
        Everyone else is TRUSTED only if the channel's trusted_users lists them."""
        is_dm = is_direct_message_channel(channel)
        if user and (user == self._profile.owner_user_id or user in self._profile.controller_user_ids):
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
        """True in the owner's own DM, or in a channel that opted into
        `full_authority` -- a channel the owner runs as their own console. Only
        the owner (which includes controller_user_ids) ever gets it; the flag
        does nothing for anyone else."""
        if principal.trust is not TrustLevel.OWNER:
            return False
        if principal.is_direct_message:
            return True
        return self._channel_flag(principal.channel, "full_authority")

    def channel_tools_for(self, principal: Principal) -> tuple[str, ...]:
        """Extra tools this channel grants this user: `extra_tools` for every
        turn, then -- for a non-owner only -- the user's `user_tools` and the
        airflow_ctl wrapper when `airflow_admins` lists them (mametchi
        channel_extra_tools, is_airflow_admin). The owner gets only
        `extra_tools`: owner tools already cover the rest (bot.py:110).

        A bare entry in OPERATOR_TOOLS (Bash, Edit, Write, NotebookEdit) only
        reaches a non-owner when the channel's `operator_full` is on. Without
        it the name is dropped here, before ToolPolicy ever sees it -- so an
        admin naming "Bash" for someone in an ordinary channel does nothing,
        rather than opening the tool-name axis while the settings-layer deny
        (engine/claude.py) still blocks it. That mismatch between the two axes
        is the 2026-10-02~10-14 mametchi bug this flag is named after; tying
        both to the same flag keeps them from drifting apart again. A scoped
        grant like `Bash(ops_ctl.sh:*)` is a different string from `Bash` and
        is never dropped here.
        """
        config = self._channels.get(principal.channel)
        if not config:
            return ()
        if principal.trust is TrustLevel.OWNER:
            return config.extra_tools
        names = list(config.user_tools.get(principal.user_id, ()))
        names += config.extra_tools
        ctl = self._profile.airflow_ctl
        if ctl is not None and principal.user_id and principal.user_id in config.airflow_admins:
            names.append(f"Bash({ctl}:*)")
        names = list(dict.fromkeys(names))
        if config.operator_full:
            return tuple(names)
        return tuple(name for name in names if name not in OPERATOR_TOOLS)

    def operator_level(self, principal: Principal, *, is_bot: bool = False) -> str:
        """OPERATOR_FULL, OPERATOR or "" for a non-owner in this channel.

        A user the channel's `operators` names (or anyone, under "*") may make
        the bot act: ToolPolicy hands them the owner tools, holding the bare
        write tools back unless the channel is `operator_full` (mametchi
        build_command can_act / full_op). A bot is never an operator, and the
        owner needs no level -- owner tools already apply.
        """
        if principal.trust is TrustLevel.OWNER:
            return ""
        config = self._channels.get(principal.channel)
        if not is_channel_operator(config, principal.user_id, is_bot=is_bot):
            return ""
        return OPERATOR_FULL if config and config.operator_full else OPERATOR

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
