"""Admin commands that change channel settings.

Writes always go through `ChannelRegistry.update`/`remove` rather than
touching the JSON directly, since a raw write would drop unknown keys used
by plugins.

Coach mode maps to `mode="agent_coach"` plus `answer_unaddressed=False`
(this core has no separate `mention_only` field, just the inverse
`answer_unaddressed`, "reply only when named"). It's set explicitly so a
channel with `answer_unaddressed=True` already stored gets reverted too.
"""

from __future__ import annotations

from typing import ClassVar

from ..observability.notices import NoticeCatalog, NoticeKey
from .command import AdminCommand, AdminContext, AdminResult


class _ChatLevelCommand(AdminCommand):
    _aliases: ClassVar[tuple[str, ...]] = ()
    _level: ClassVar[str] = ""
    _notice_key: ClassVar[NoticeKey]

    def __init__(self, notices: NoticeCatalog) -> None:
        self._notices = notices

    def matches(self, text: str) -> bool:
        return text.strip() in self._aliases

    def execute(self, ctx: AdminContext) -> AdminResult:
        ctx.channels.update(ctx.channel, {"chat": self._level})
        return AdminResult(message=self._notices.render(self._notice_key))


class ChatActiveCommand(_ChatLevelCommand):
    name: ClassVar[str] = "chat_active"
    _aliases = ("말수 많게", "적극 모드", "말 많이", "수다 모드")
    _level = "active"
    _notice_key = NoticeKey.CHAT_ACTIVE


class ChatNormalCommand(_ChatLevelCommand):
    name: ClassVar[str] = "chat_normal"
    _aliases = ("말수 보통", "기본 말수")
    _level = "normal"
    _notice_key = NoticeKey.CHAT_NORMAL


class ChatQuietCommand(_ChatLevelCommand):
    name: ClassVar[str] = "chat_quiet"
    _aliases = ("말수 적게", "조용 모드", "조용히")
    _level = "quiet"
    _notice_key = NoticeKey.CHAT_QUIET


class CoachModeCommand(AdminCommand):
    name: ClassVar[str] = "coach_mode"

    def __init__(self, notices: NoticeCatalog) -> None:
        self._notices = notices

    def matches(self, text: str) -> bool:
        return text.strip() in ("코치 모드", "코치모드")

    def execute(self, ctx: AdminContext) -> AdminResult:
        ctx.channels.update(
            ctx.channel,
            {"mode": "agent_coach", "answer_unaddressed": False, "light_context": True},
        )
        return AdminResult(message=self._notices.render(NoticeKey.MODE_MENTION_ONLY))


class ApiModeCommand(AdminCommand):
    name: ClassVar[str] = "api_mode"

    def __init__(self, notices: NoticeCatalog) -> None:
        self._notices = notices

    def matches(self, text: str) -> bool:
        return text.strip() in ("api 모드", "api모드", "API 모드")

    def execute(self, ctx: AdminContext) -> AdminResult:
        ctx.channels.update(ctx.channel, {"mode": "api_helpdesk"})
        return AdminResult(message=self._notices.render(NoticeKey.MODE_STRUCTURED))


class DefaultModeCommand(AdminCommand):
    name: ClassVar[str] = "default_mode"

    def __init__(self, notices: NoticeCatalog) -> None:
        self._notices = notices

    def matches(self, text: str) -> bool:
        return text.strip() in ("기본 모드", "기본모드")

    def execute(self, ctx: AdminContext) -> AdminResult:
        ctx.channels.update(ctx.channel, {"mode": "private"})
        return AdminResult(message=self._notices.render(NoticeKey.MODE_PLAIN))


class ChannelUnregisterCommand(AdminCommand):
    name: ClassVar[str] = "channel_unregister"

    def __init__(self, notices: NoticeCatalog) -> None:
        self._notices = notices

    def matches(self, text: str) -> bool:
        return text.strip() in ("채널 해제", "채널해제", "여기서 나가", "응답 중지")

    def execute(self, ctx: AdminContext) -> AdminResult:
        config = ctx.channels.get(ctx.channel)
        if config is None:
            return AdminResult(message=self._notices.render(NoticeKey.NOT_LISTED))
        ctx.channels.remove(ctx.channel)
        return AdminResult(message=self._notices.render(NoticeKey.CHANNEL_UNREGISTERED, name=config.name))
