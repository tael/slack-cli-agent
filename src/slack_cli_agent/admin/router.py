"""Admin command routing.

Permission is checked in exactly one place here, after `matches` picks the
command — repeating the check per command class risks one being forgotten
and quietly wide open.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from .command import AdminCommand, AdminContext, AdminResult


class AdminRouter:
    def __init__(self, commands: Sequence[AdminCommand]) -> None:
        self._commands = tuple(commands)

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
            return command.execute(replace(ctx, text=text))
        return None
