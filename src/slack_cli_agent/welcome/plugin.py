"""Hangs the welcome on the G1 hooks: the join event, a retry job and the
owner's retroactive command (G2).

Core, not an org plugin (advisor_plan 5-7): nothing here is org-specific,
and with no `"welcome": true` channel it does nothing.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any, ClassVar

from ..admin.command import AdminCommand, AdminContext, AdminResult
from ..plugin.base import BotPlugin
from ..plugin.hooks import BackgroundJob, EventHandler, HookEvent
from .service import WelcomeService, _Channels, is_member_join, welcome_enabled

_RETRO = re.compile(
    r"^\s*환영(?:\s+<#(?P<channel>[CG][A-Z0-9]+)(?:\|[^>]*)?>)?(?P<users>(?:\s+<@[UW][A-Z0-9]+(?:\|[^>]*)?>)+)\s*$"
)
_USER = re.compile(r"<@([UW][A-Z0-9]+)")


class WelcomeCommand(AdminCommand):
    """Queues a welcome for people whose join event was missed (bot.py
    process_welcome_requests). The worker job writes them, so the command
    itself answers at once."""

    name: ClassVar[str] = "welcome"
    usage: ClassVar[str] = "환영 [#채널] @사람"
    description: ClassVar[str] = "입장 알림을 놓친 사람을 소급해 환영한다"

    def __init__(self, service: Callable[[], WelcomeService]) -> None:
        self._service = service

    def matches(self, text: str) -> bool:
        return _RETRO.match(text) is not None

    def execute(self, ctx: AdminContext) -> AdminResult:
        found = _RETRO.match(ctx.text)
        if found is None:
            return AdminResult(message="환영할 사람을 멘션으로 적어 주세요.", applied=False)
        channel = found.group("channel") or ctx.channel
        users = _USER.findall(found.group("users"))
        store = self._service().store
        for user in users:
            store.request(channel, user)
        return AdminResult(message=f"<#{channel}> 에 {len(users)}명 환영을 요청했다.")


class WelcomePlugin(BotPlugin):
    name: ClassVar[str] = "welcome"

    def __init__(
        self,
        channels: _Channels,
        service: Callable[[], WelcomeService],
        *,
        retry_interval_sec: float = 30.0,
    ) -> None:
        self._channels = channels
        self._service = service
        self._retry_interval_sec = retry_interval_sec

    def _accepts(self, event: Any) -> bool:
        return is_member_join(event) and welcome_enabled(self._channels.get(str(event.get("channel") or "")))

    def _handle(self, call: HookEvent) -> None:
        self._service().welcome(str(call.event["channel"]), str(call.event["user"]), call.engine)

    def _queue(self, call: HookEvent) -> None:
        # No slot in time: the retry job welcomes them on its next round.
        self._service().store.request(str(call.event["channel"]), str(call.event["user"]))

    def event_handlers(self) -> Sequence[EventHandler]:
        return (EventHandler(
            event_type="message",
            name="join",
            handle=self._handle,
            accepts=self._accepts,
            # The joiner is the subject, not a requester; the channel opted in.
            audience="all",
            on_unavailable=self._queue,
        ),)

    def background_jobs(self) -> Sequence[BackgroundJob]:
        return (BackgroundJob(
            name="retro",
            run=lambda engine: self._service().drain(engine),
            interval_sec=self._retry_interval_sec,
            initial_delay_sec=12.0,
        ),)

    def admin_commands(self) -> Sequence[AdminCommand]:
        return (WelcomeCommand(self._service),)
