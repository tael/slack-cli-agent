"""Requester identity: channel and trust level bundled into one value object."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum


class TrustLevel(IntEnum):
    """Trust level; higher values mean broader permissions.

    GENERAL only gets what the channel allows. TRUSTED is on the channel's
    trusted_users list. OWNER matches the profile's owner_user_id and gets
    elevated model/effort regardless of channel. A controller bot is never
    OWNER; a delegated controller turn is GENERAL with `delegated` set.
    """

    GENERAL = 0
    TRUSTED = 1
    OWNER = 2


@dataclass(frozen=True)
class Principal:
    user_id: str
    channel: str
    trust: TrustLevel
    is_direct_message: bool
    #: Listed in the profile's controller_user_ids: a bot account whose
    #: requests are accepted. Says nothing about what the turn may do.
    is_controller: bool = False
    #: A controller's message the delegation ledger names as handed to this
    #: bot. Trust stays where it is; only the tool axis widens (auth/tools.py).
    delegated: bool = False


class TurnTier(StrEnum):
    """What one turn actually ran as, finer than TrustLevel (A12).

    TrustLevel says who spoke; the tier says what the turn was handed. The
    model only sees the tools it got and guessed the rest, so it wrote "there
    is no way to do that" for a tool that was merely out of this turn's reach
    (mametchi 2026-10-14). The same nine values as mametchi authz.tier, so the
    audit rows of both bots read alike. DELEGATED comes from
    Principal.delegated (the delegation ledger).
    """

    READONLY = "readonly"
    OWNER_TRUSTED = "owner_trusted"
    OWNER = "owner"
    DELEGATED = "delegated"
    BOT = "bot"
    OPERATOR_FULL = "operator_full"
    OPERATOR = "operator"
    AIRFLOW = "airflow"
    READ = "read"
