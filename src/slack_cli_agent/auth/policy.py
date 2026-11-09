"""Permission layer bundling what were separate judgment functions under a
single `Principal` input.

Company-specific rules aren't here — only the `AccessExtension` extension
point; plugins implement the actual logic.
"""

from __future__ import annotations

import logging
from abc import ABC
from collections.abc import Sequence
from typing import TYPE_CHECKING

from ..config.channel import advisor_enabled, airflow_admins, is_channel_operator
from ..core.channel_kind import is_direct_message_channel
from .principal import Principal, TrustLevel, TurnTier

if TYPE_CHECKING:
    from ..config.channel import ChannelConfig, ChannelRegistry
    from ..config.profile import Profile
    from .delegation import DelegationLedger

log = logging.getLogger(__name__)

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


def turn_tier(
    principal: Principal,
    config: ChannelConfig | None,
    *,
    full_authority: bool,
    is_bot: bool = False,
    delegated: bool = False,
    readonly: bool = False,
) -> TurnTier:
    """The tier this turn runs as, in mametchi authz.tier's order (A12).

    readonly first: a postmortem or watch check strips tools whoever asked.
    The owner splits on full authority (own DM or a full_authority channel).
    A bot is never an operator, so it is decided before the operator lists.
    """
    if readonly:
        return TurnTier.READONLY
    if principal.trust is TrustLevel.OWNER:
        return TurnTier.OWNER_TRUSTED if full_authority else TurnTier.OWNER
    if delegated:
        return TurnTier.DELEGATED
    if is_bot:
        return TurnTier.BOT
    if is_channel_operator(config, principal.user_id):
        return TurnTier.OPERATOR_FULL if config and config.operator_full else TurnTier.OPERATOR
    if principal.user_id and principal.user_id in airflow_admins(config):
        return TurnTier.AIRFLOW
    return TurnTier.READ


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
        delegation: DelegationLedger | None = None,
    ) -> None:
        self._profile = profile
        self._channels = channels
        self._extensions = tuple(extensions)
        self._delegation = delegation

    def principal_for(self, channel: str, user: str, message_ts: str = "") -> Principal:
        """The owner is OWNER regardless of channel. Everyone else is TRUSTED
        only if the channel's trusted_users lists them.

        controller_user_ids are accepted (accepts_request) but not promoted:
        a controller acts only for a message the delegation ledger vouches
        for, and then it gets the channel operator's tool set
        (operator_level), not owner trust. With
        no ledger configured every controller turn is plain GENERAL.
        """
        is_dm = is_direct_message_channel(channel)
        is_controller = bool(user) and user in self._profile.controller_user_ids
        if user and user == self._profile.owner_user_id:
            trust = TrustLevel.OWNER
        elif user and self._is_channel_trusted(channel, user):
            trust = TrustLevel.TRUSTED
        else:
            trust = TrustLevel.GENERAL
        delegated = is_controller and self._is_delegated(channel, message_ts, user)
        return Principal(
            user_id=user, channel=channel, trust=trust, is_direct_message=is_dm,
            is_controller=is_controller, delegated=delegated,
        )

    def _is_delegated(self, channel: str, message_ts: str, user: str = "") -> bool:
        if self._delegation is None or not message_ts:
            return False
        try:
            found = self._delegation.lookup_settled(channel, message_ts, user)
        except Exception:  # a broken ledger must close, not open, the turn
            log.exception("위임 원장 판정에 실패해 위임 없음으로 본다 : %s", message_ts)
            return False
        if found is not None:
            log.info("위임 원장에 있는 요청이라 위임 도구를 붙인다 : %s : %s", channel, message_ts)
        return found is not None

    def accepts_request(self, principal: Principal) -> bool:
        """Whether this conversation gets an answer at all (bot.py:3695).

        The owner passes anywhere. Any other DM is refused, and a channel
        passes only if it is registered. An empty owner_user_id makes nobody
        the owner, which is stricter than the original -- there `user` and
        `OWNER_USER_ID` both being empty would have passed.
        """
        if principal.trust is TrustLevel.OWNER or principal.is_controller:
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
        """Model to use, the same for every requester (mametchi model_for,
        bot.py). An advisor channel runs on its executor model, then the
        channel's model, then the engine default. The owner is not forced to
        the owner model any more: that ran chat channels on the big model and
        kept a channel's model from reaching the owner (김태일, 2026-10-22).
        Reviews pick their own model (review/engine_adapter.py)."""
        config = self._channels.get(principal.channel)
        default = self._profile.primary_engine.model
        if config is not None and advisor_enabled(config):
            # Same as bot.py advisor_executor: an empty executor falls back
            # to the default model, not to the channel's model.
            return str(config.extra["advisor"].get("executor") or default)
        if config and config.model:
            return config.model
        return default

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
        the owner ever gets it -- not a controller bot, delegated or not; the
        flag does nothing for anyone else."""
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
        the bot act: ToolPolicy hands them the profile's operator tools, holding
        the bare write tools back unless the channel is `operator_full` (mametchi
        build_command can_act / full_op). A bot is never an operator by the
        list; a controller bot is one only for a message the delegation ledger
        vouches for (mametchi bot.py:7855 operator=bool(delegated) or
        is_operator). The owner needs no level -- owner tools already apply.
        """
        if principal.trust is TrustLevel.OWNER:
            return ""
        config = self._channels.get(principal.channel)
        if not principal.delegated and not is_channel_operator(
            config, principal.user_id, is_bot=is_bot,
        ):
            return ""
        # A delegated turn is work the owner handed over through a controller
        # bot in the owner's own turn, so it runs with the shell regardless of
        # the channel's operator_full. 2026-11-08 test-weather-simul : the
        # ledger vouched for 12:39 마메치 위임 (tier=delegated) but the channel
        # had no operator_full, bare Bash was held back and the turn answered
        # "don't ask mode" twice. 김태일 지시로 위임 턴은 셸을 연다.
        if principal.delegated:
            return OPERATOR_FULL
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
