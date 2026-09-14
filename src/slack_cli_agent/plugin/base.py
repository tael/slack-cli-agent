"""Per-bot extension points.

`BotPlugin` is the only way to keep org-specific features (internal
workflow-tool integration, an internal API helpdesk mode, a coach mode) out
of core code. Every hook defaults to an empty sequence, so a plugin only
overrides what it needs.
"""

from __future__ import annotations

from abc import ABC
from collections.abc import Sequence
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from ..admin.command import AdminCommand
    from ..auth.policy import AccessExtension
    from ..engine.base import Engine
    from ..guard.base import OutputGuard
    from ..preflight.check import PreflightCheck
    from ..prompt.sections import PromptSection


class BotPlugin(ABC):
    name: ClassVar[str]

    def access_extensions(self) -> Sequence[AccessExtension]:
        return ()

    def admin_commands(self) -> Sequence[AdminCommand]:
        return ()

    def prompt_sections(self) -> Sequence[PromptSection]:
        return ()

    def output_guards(self) -> Sequence[OutputGuard]:
        return ()

    def preflight_checks(self) -> Sequence[PreflightCheck]:
        return ()

    def engines(self) -> Sequence[type[Engine]]:
        """Engine types this plugin adds. Kept as an extension point, like
        access policy and admin commands, rather than hardcoded — otherwise
        a caller couldn't plug in its own engine."""
        return ()
