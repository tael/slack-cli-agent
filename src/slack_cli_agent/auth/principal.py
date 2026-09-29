"""Requester identity: channel and trust level bundled into one value object."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class TrustLevel(IntEnum):
    """Trust level; higher values mean broader permissions.

    GENERAL only gets what the channel allows. TRUSTED is on the channel's
    trusted_users list. OWNER matches the profile's owner_user_id and gets
    elevated model/effort regardless of channel.
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
