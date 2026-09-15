"""Admin command contract.

Each command is a class; permission checks live in `AdminRouter`, not in
individual commands — repeating the check per command risks forgetting one.
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
    principal: Principal
    channel: str
    thread_ts: str
    channels: ChannelRegistry
    profile: Profile
    text: str = ""
    """Raw command body, filled in by the router.

    Most commands never read this since `matches` already decided; only
    commands with inline arguments (like revert) need it.
    """

    help_text: str = ""
    """Command list, filled in by the router. Only HelpCommand reads it."""


@dataclass(frozen=True)
class AdminResult:
    """The reply text shown to the user as-is."""

    message: str
    handled: bool = True
    """False means the command matched but was denied for lack of permission."""


class AdminCommand(ABC):
    name: ClassVar[str]
    required_trust: ClassVar[TrustLevel] = TrustLevel.OWNER

    #: What the user types, and what it does. The help listing is built
    #: from these, so a command added without them can't be discovered.
    usage: ClassVar[str] = ""
    description: ClassVar[str] = ""

    @abstractmethod
    def matches(self, text: str) -> bool:
        pass

    @abstractmethod
    def execute(self, ctx: AdminContext) -> AdminResult:
        """Runs the command, assuming the router already checked permission."""
