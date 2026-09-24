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
    usage: ClassVar[str] = "말수 많게"
    description: ClassVar[str] = "이 채널에서 더 자주 나선다"
    _aliases = ("말수 많게", "적극 모드", "말 많이", "수다 모드")
    _level = "active"
    _notice_key = NoticeKey.CHAT_ACTIVE


class ChatNormalCommand(_ChatLevelCommand):
    name: ClassVar[str] = "chat_normal"
    usage: ClassVar[str] = "말수 보통"
    description: ClassVar[str] = "이 채널의 말수를 기본으로 되돌린다"
    _aliases = ("말수 보통", "기본 말수")
    _level = "normal"
    _notice_key = NoticeKey.CHAT_NORMAL


class ChatQuietCommand(_ChatLevelCommand):
    name: ClassVar[str] = "chat_quiet"
    usage: ClassVar[str] = "말수 적게"
    description: ClassVar[str] = "이 채널에서 덜 나선다"
    _aliases = ("말수 적게", "조용 모드", "조용히")
    _level = "quiet"
    _notice_key = NoticeKey.CHAT_QUIET


class _UnaddressedCommand(AdminCommand):
    """Toggles `answer_unaddressed` for the channel the command came from.

    Until this existed the only way to change it was hand-editing
    channels.json, so the setting was effectively invisible from Slack.
    """

    _aliases: ClassVar[tuple[str, ...]] = ()
    _value: ClassVar[bool] = False
    _notice_key: ClassVar[NoticeKey]

    def __init__(self, notices: NoticeCatalog) -> None:
        self._notices = notices

    def matches(self, text: str) -> bool:
        return text.strip() in self._aliases

    def execute(self, ctx: AdminContext) -> AdminResult:
        ctx.channels.update(ctx.channel, {"answer_unaddressed": self._value})
        return AdminResult(message=self._notices.render(self._notice_key))


class UnaddressedOnCommand(_UnaddressedCommand):
    name: ClassVar[str] = "unaddressed_on"
    usage: ClassVar[str] = "끼어들기 허용"
    description: ClassVar[str] = "이 채널에서 이름을 안 불러도 답한다"
    _aliases = ("끼어들기 허용", "끼어들기허용", "호명 없이 응답", "멘션 없이 응답")
    _value = True
    _notice_key = NoticeKey.UNADDRESSED_ON


class UnaddressedOffCommand(_UnaddressedCommand):
    name: ClassVar[str] = "unaddressed_off"
    usage: ClassVar[str] = "멘션 전용"
    description: ClassVar[str] = "이 채널에서 이름을 불러야만 답한다"
    _aliases = ("멘션 전용", "멘션전용", "호명 전용", "불러야 답해")
    _value = False
    _notice_key = NoticeKey.UNADDRESSED_OFF


class CoachModeCommand(AdminCommand):
    name: ClassVar[str] = "coach_mode"
    usage: ClassVar[str] = "코치 모드"
    description: ClassVar[str] = "이 채널을 에이전트 코치 형식으로 바꾼다"

    def __init__(self, notices: NoticeCatalog) -> None:
        self._notices = notices

    def matches(self, text: str) -> bool:
        return text.strip() in ("코치 모드", "코치모드", "에이전트 코치")

    def execute(self, ctx: AdminContext) -> AdminResult:
        ctx.channels.update(
            ctx.channel,
            {"mode": "agent_coach", "answer_unaddressed": False, "light_context": True},
        )
        return AdminResult(message=self._notices.render(NoticeKey.MODE_MENTION_ONLY))


class DefaultModeCommand(AdminCommand):
    name: ClassVar[str] = "default_mode"
    usage: ClassVar[str] = "기본 모드"
    description: ClassVar[str] = "이 채널을 일반 응답 형식으로 되돌린다"

    def __init__(self, notices: NoticeCatalog) -> None:
        self._notices = notices

    def matches(self, text: str) -> bool:
        return text.strip() in ("기본 모드", "기본모드")

    def execute(self, ctx: AdminContext) -> AdminResult:
        ctx.channels.update(ctx.channel, {"mode": "private"})
        return AdminResult(message=self._notices.render(NoticeKey.MODE_PLAIN))


class ChannelUnregisterCommand(AdminCommand):
    name: ClassVar[str] = "channel_unregister"
    usage: ClassVar[str] = "채널 해제"
    description: ClassVar[str] = "지금 이 채널을 응답 목록에서 뺀다"

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
