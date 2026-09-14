"""채널 설정을 바꾸는 관리 명령.

원본 `handle_admin`(01-source-analysis.md 18절, bot.py:3836-3960) 중 채널
단위 설정을 쓰는 것만 모은다. 쓰기는 전부 `ChannelRegistry.update`/`remove`
를 거친다 — 직접 JSON 을 만지면 플러그인이 쓰는 알 수 없는 키가 지워진다.

원본의 "코치 모드" 는 `mode="agent_coach"` 와 함께 `mention_only=True` 를
켠다. 이 코어에는 `mention_only` 필드가 없고 그 반대 뜻으로 이미 있는
`answer_unaddressed`(기본값 False, "이름을 불러야 답한다") 가 대응한다
(`slack/listener.py` 참고). 그래서 여기서는 `answer_unaddressed=False` 를
명시한다 — 채널에 이미 True 로 저장돼 있던 경우까지 되돌리기 위해서다.

원본에는 조직 제품명을 그대로 딴 별칭이 하나 더 있다. 그것은 여기 넣지
않는다. "코치 모드"/"코치모드" 만 받는다.
"""

from __future__ import annotations

from typing import ClassVar

from ..observability.notices import NoticeCatalog, NoticeKey
from .command import AdminCommand, AdminContext, AdminResult


class _ChatLevelCommand(AdminCommand):
    """말수를 바꾸는 명령의 공통 동작. 별칭과 값만 하위 클래스가 정한다."""

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
    """말수를 늘린다. 원본 별칭 네 가지를 그대로 받는다."""

    name: ClassVar[str] = "chat_active"
    _aliases = ("말수 많게", "적극 모드", "말 많이", "수다 모드")
    _level = "active"
    _notice_key = NoticeKey.CHAT_ACTIVE


class ChatNormalCommand(_ChatLevelCommand):
    """말수를 기본으로 되돌린다."""

    name: ClassVar[str] = "chat_normal"
    _aliases = ("말수 보통", "기본 말수")
    _level = "normal"
    _notice_key = NoticeKey.CHAT_NORMAL


class ChatQuietCommand(_ChatLevelCommand):
    """말수를 줄인다."""

    name: ClassVar[str] = "chat_quiet"
    _aliases = ("말수 적게", "조용 모드", "조용히")
    _level = "quiet"
    _notice_key = NoticeKey.CHAT_QUIET


class CoachModeCommand(AdminCommand):
    """코치 모드. 멘션 전용 + 짧은 맥락으로 전환한다."""

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
    """지정된 응답 형식(연동 문의 대응)으로 전환한다."""

    name: ClassVar[str] = "api_mode"

    def __init__(self, notices: NoticeCatalog) -> None:
        self._notices = notices

    def matches(self, text: str) -> bool:
        return text.strip() in ("api 모드", "api모드", "API 모드")

    def execute(self, ctx: AdminContext) -> AdminResult:
        ctx.channels.update(ctx.channel, {"mode": "api_helpdesk"})
        return AdminResult(message=self._notices.render(NoticeKey.MODE_STRUCTURED))


class DefaultModeCommand(AdminCommand):
    """기본 응답 형식으로 되돌린다."""

    name: ClassVar[str] = "default_mode"

    def __init__(self, notices: NoticeCatalog) -> None:
        self._notices = notices

    def matches(self, text: str) -> bool:
        return text.strip() in ("기본 모드", "기본모드")

    def execute(self, ctx: AdminContext) -> AdminResult:
        ctx.channels.update(ctx.channel, {"mode": "private"})
        return AdminResult(message=self._notices.render(NoticeKey.MODE_PLAIN))


class ChannelUnregisterCommand(AdminCommand):
    """채널을 응답 목록에서 뺀다. 등록 안 된 채널이면 그 사실을 답한다."""

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
