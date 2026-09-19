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
from ..slack.reactions import ReactionMarker
from ..storage.admin_claims import AdminClaims
from .command import AdminContext, AdminResult

log = logging.getLogger(__name__)


class ClaimUnavailable(Exception):
    """The claim ledger could not be written, so this process must not run
    the command -- another one may run it too. The caller decides recovery:
    the socket path can ask the user to try again, catch-up can leave it for
    the next sweep."""

ReplyCallback = Callable[[str, str, str], None]
AdminContextBuilder = Callable[[RequestContext], AdminContext]


class AdminDispatcher(Protocol):
    def matches(self, text: str) -> bool: ...

    def dispatch(self, text: str, ctx: AdminContext) -> AdminResult | None: ...


class AdminAdmission:
    def __init__(
        self,
        router: AdminDispatcher,
        context_builder: AdminContextBuilder,
        reply: ReplyCallback,
        # None means no marking: a missing injection must not stop commands
        # from being handled.
        markers: ReactionMarker | None = None,
        # None keeps the old behavior: the two processes are not coordinated
        # and can run the same command twice.
        claims: AdminClaims | None = None,
        owner: str = "",
    ) -> None:
        self._router = router
        self._context_builder = context_builder
        self._reply = reply
        self._markers = markers
        self._claims = claims
        self._owner = owner

    def handled(self, ctx: RequestContext) -> bool:
        """True means the request is done and must not reach the model.

        A denied command counts as handled: passing it on would run as a
        model request what the permission check just refused.
        """
        if not self._router.matches(ctx.text):
            return False
        # The claim is a DB write, so it is taken only once the text is known
        # to be a command -- every request paying for one would put the socket
        # handler thread on the database lock (sca-9l1).
        if not self._claimed(ctx):
            # Someone else already ran it and posted the reply. Reported as
            # handled so it does not reach the model, and left unmarked --
            # the process that ran it marks it.
            return True
        try:
            result = self._router.dispatch(ctx.text, self._context_builder(ctx))
        except Exception as exc:  # noqa: BLE001 - see below
            # The dispatch may have applied part of the command before
            # raising, so this is closed as failed rather than reopened.
            log.warning("관리 명령 처리 실패 : %s:%s : %s", ctx.channel, ctx.ts, exc)
            if self._close(ctx, ok=False, failure=str(exc)) and self._markers is not None:
                self._markers.mark_failed(ctx.channel, ctx.ts)
            return True
        if result is None:
            # Matched but produced nothing. Closed rather than left running,
            # or the stale sweep would report it as a dead process later.
            self._close(ctx, ok=True)
            return False
        try:
            self._reply(ctx.channel, ctx.thread_ts, result.message)
        except Exception as exc:  # noqa: BLE001 - a failed post must not send the command to the model
            log.warning("관리 명령 답을 보내지 못했다 : %s:%s : %s", ctx.channel, ctx.ts, exc)
        # Nothing else marks a command -- it never enters the queue. Catch-up
        # reads the marks, not this process's memory, so without one it finds
        # the message again and runs the command a second time. The original
        # marks it here too (bot.py:4989) (sca-sk9t).
        #
        # Only when this process still owns the claim: the stale sweep may
        # have closed it as failed and posted x, and overwriting that with a
        # done mark would undo the sweep (codex review).
        if self._close(ctx, ok=True) and self._markers is not None:
            self._markers.mark_done(ctx.channel, ctx.ts)
        return True

    def _claimed(self, ctx: RequestContext) -> bool:
        if self._claims is None:
            return True
        try:
            return self._claims.claim(ctx.channel, ctx.ts, owner=self._owner)
        except Exception as exc:
            # Running it anyway would let the other process run it too, which
            # is exactly what the ledger exists to stop. Raised rather than
            # swallowed: leaving it unmarked is not recovery by itself, since
            # the socket path has already recorded the event as seen and
            # catch-up only reaches back one window (codex review).
            log.warning("관리 명령 점유 실패로 실행하지 않는다 : %s:%s : %s", ctx.channel, ctx.ts, exc)
            raise ClaimUnavailable(str(exc)) from exc

    def _close(self, ctx: RequestContext, *, ok: bool, failure: str = "") -> bool:
        """False means this process no longer owns the claim, so it must not
        touch the mark either."""
        if self._claims is None:
            return True
        try:
            return self._claims.finish(
                ctx.channel, ctx.ts, owner=self._owner, ok=ok, failure=failure
            )
        except Exception as exc:  # noqa: BLE001 - the command already ran
            log.warning("관리 명령 점유 종료 실패 : %s:%s : %s", ctx.channel, ctx.ts, exc)
            return False
