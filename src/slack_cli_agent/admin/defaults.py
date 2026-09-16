"""The admin commands every bot gets.

Kept out of Application so the help-coverage test can build the same list
without constructing a whole Application.
"""

from __future__ import annotations

from ..observability.notices import NoticeCatalog
from .channel_commands import (
    ChannelUnregisterCommand,
    ChatActiveCommand,
    ChatNormalCommand,
    ChatQuietCommand,
    CoachModeCommand,
    DefaultModeCommand,
    UnaddressedOffCommand,
    UnaddressedOnCommand,
)
from .command import AdminCommand
from .commands import ChannelListCommand, EngineStatusCommand, HelpCommand
from .engine_commands import EngineApproveCommand, EngineDenyCommand
from .learning_commands import LearningApplyCommand, LearningRevertCommand, LearningShowCommand


def default_admin_commands(notices: NoticeCatalog) -> list[AdminCommand]:
    return [
        HelpCommand(),
        ChannelListCommand(),
        EngineStatusCommand(),
        EngineApproveCommand(),
        EngineDenyCommand(),
        ChatActiveCommand(notices),
        ChatNormalCommand(notices),
        ChatQuietCommand(notices),
        UnaddressedOnCommand(notices),
        UnaddressedOffCommand(notices),
        CoachModeCommand(notices),
        DefaultModeCommand(notices),
        ChannelUnregisterCommand(notices),
        LearningShowCommand(),
        LearningApplyCommand(),
        LearningRevertCommand(),
    ]
