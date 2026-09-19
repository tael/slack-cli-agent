"""Admin command routing.

Permission is checked in exactly one place here, after `matches` picks the
command — repeating the check per command class risks one being forgotten
and quietly wide open.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace

from ..core.errors import ConfigError
from .command import AdminCommand, AdminContext, AdminResult

log = logging.getLogger(__name__)


class AdminRouter:
    def __init__(self, commands: Sequence[AdminCommand]) -> None:
        self._commands = tuple(commands)

    def help_text(self) -> str:
        """Command listing built from the registered commands.

        A hardcoded listing went stale — 15 commands were wired and the
        help named 3 (2026-09-15), so the rest were undiscoverable.
        """
        lines = [
            f"- {c.usage} : {c.description}"
            for c in self._commands
            if c.usage and c.description
        ]
        return "\n".join(lines)

    def matches(self, text: str) -> bool:
        """Whether any command would take this text. Lets a caller decide
        before building context or writing a claim row, without running the
        command (sca-8m5p)."""
        return any(c.matches(text) for c in self._commands)

    def dispatch(self, text: str, ctx: AdminContext) -> AdminResult | None:
        """Returns None when nothing matches, so the caller falls through to
        a normal request. A match with insufficient permission returns a
        result instead of None, so it isn't silently retried as a normal
        request routed to the model.
        """
        for command in self._commands:
            if not command.matches(text):
                continue
            if ctx.principal.trust < command.required_trust:
                return AdminResult(
                    message="이 명령은 권한이 없어 실행할 수 없습니다.",
                    handled=False,
                )
            # Filled in here rather than by every call site building context,
            # so no call site can forget it and break the command's argument
            # parsing.
            try:
                return command.execute(replace(ctx, text=text, help_text=self.help_text()))
            except ConfigError as exc:
                # The ingress swallows exceptions and only logs them, so a
                # command that stops on a bad config file would look to the
                # user exactly like one that worked (sca-zvk).
                log.warning("관리 명령 중단 : %s : %s", command.name, exc)
                return AdminResult(message=f"설정을 읽지 못해 실행하지 않았습니다. {exc}", handled=False)
        return None
