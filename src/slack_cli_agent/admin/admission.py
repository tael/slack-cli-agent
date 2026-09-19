"""Decides whether an incoming request is an admin command, in one place.

The socket path ran this check and the catch-up path did not, so an admin
command that arrived while the bot was down came back as a normal request
and went to the model (sca-oyku). The original has one entry point for
both: catch-up calls handle_request, which runs handle_admin first
(bot.py:4990, 7082).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

from ..core.context import RequestContext
from .command import AdminContext, AdminResult

log = logging.getLogger(__name__)

ReplyCallback = Callable[[str, str, str], None]
AdminContextBuilder = Callable[[RequestContext], AdminContext]


class AdminDispatcher(Protocol):
    def dispatch(self, text: str, ctx: AdminContext) -> AdminResult | None: ...


class AdminAdmission:
    def __init__(
        self,
        router: AdminDispatcher,
        context_builder: AdminContextBuilder,
        reply: ReplyCallback,
    ) -> None:
        self._router = router
        self._context_builder = context_builder
        self._reply = reply

    def handled(self, ctx: RequestContext) -> bool:
        """True means the request is done and must not reach the model.

        A denied command counts as handled: passing it on would run as a
        model request what the permission check just refused.
        """
        result = self._router.dispatch(ctx.text, self._context_builder(ctx))
        if result is None:
            return False
        try:
            self._reply(ctx.channel, ctx.thread_ts, result.message)
        except Exception as exc:  # noqa: BLE001 - a failed post must not send the command to the model
            log.warning("관리 명령 답을 보내지 못했다 : %s:%s : %s", ctx.channel, ctx.ts, exc)
        return True
