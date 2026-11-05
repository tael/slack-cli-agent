"""Whether an incoming request is answered at all, and the owner's automatic
channel registration that goes with it.

Both come from the original's single entry point: `is_allowed` (bot.py:3695)
called at bot.py:4969, and the registration at bot.py:4996 that runs after
admin dispatch. They ship together on purpose -- the check alone would silence
every unregistered channel with no way back in.

The judgment itself is `AccessPolicy.accepts_request`; this class only pairs it
with the registration and the notice, which need Slack.

Someone other than the owner who calls or invites the bot in an unregistered
channel gets the owner called in that channel (`call_owner`, ported from the
original's notify_unregistered_channel, 2026-11-05). Before that a refusal
there was silent and the caller took the bot for broken.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any, Literal

from ..auth.policy import AccessPolicy
from ..auth.principal import TrustLevel
from ..config.channel import ChannelRegistry
from ..observability.notices import NoticeCatalog, NoticeKey
from .channel_kind import is_direct_message_channel
from .unregistered import UnregisteredNoticeLog

log = logging.getLogger(__name__)

# (channel, thread_ts, body)
ReplyCallback = Callable[[str, str, str], None]
ChannelName = Callable[[str], str]
AttemptKind = Literal["mention", "invite"]


class RequestAccess:
    def __init__(
        self,
        policy: AccessPolicy,
        channels: ChannelRegistry,
        channel_name: ChannelName,
        notices: NoticeCatalog,
        reply: ReplyCallback,
        owner_user_id: str = "",
        bot_name: str = "",
        self_user_id: Callable[[], str] = lambda: "",
        # None leaves a refusal in an unregistered channel silent.
        notice_log: UnregisteredNoticeLog | None = None,
        # Slack does not notify a non-member mentioned in a private channel.
        notify_owner: Callable[[str], bool] | None = None,
    ) -> None:
        self._policy = policy
        self._channels = channels
        self._channel_name = channel_name
        self._notices = notices
        self._reply = reply
        self._owner_user_id = owner_user_id
        self._bot_name = bot_name
        self._self_user_id = self_user_id
        self._notice_log = notice_log
        self._notify_owner = notify_owner

    def allows(self, channel: str, user: str) -> bool:
        """The check itself says nothing; a mention in an unregistered channel
        is answered separately by `call_owner`."""
        principal = self._policy.principal_for(channel, user)
        if self._policy.accepts_request(principal):
            return True
        log.info("허용되지 않은 대화에서 온 요청이라 무시한다 : %s : %s", channel, user)
        return False

    def register_owner_channel(self, channel: str, user: str, thread_ts: str) -> None:
        """Registers the channel the owner just called in, so other people can
        use the bot there too. DMs are left out: every DM would otherwise land
        in the channel file, and a DM needs no registration to work.
        """
        principal = self._policy.principal_for(channel, user)
        if principal.trust is not TrustLevel.OWNER or principal.is_direct_message:
            return
        if not self._channels.register(channel, self._channel_name(channel)):
            return
        log.info("소유자 호출로 채널을 등록했다 : %s", channel)
        try:
            self._reply(channel, thread_ts, self._notices.render(NoticeKey.JOINED))
        except Exception as exc:  # noqa: BLE001 - the channel is registered; losing the notice must not lose the request
            log.warning("채널 등록 안내를 보내지 못했다 : %s : %s", channel, exc)

    def self_join(self, event: Mapping[str, Any]) -> tuple[str, str] | None:
        """(channel, inviter) when the event is this bot joining a channel.
        An empty inviter means Slack did not say who invited it."""
        bot = self._self_user_id()
        if not bot or event.get("subtype") != "channel_join" or event.get("user") != bot:
            return None
        return str(event.get("channel") or ""), str(event.get("inviter") or "")

    def call_owner(self, channel: str, user: str, thread_ts: str, kind: AttemptKind) -> None:
        """Calls the owner in an unregistered channel where someone else
        mentioned or invited the bot, at most once per channel per cooldown."""
        if self._notice_log is None or not self._owner_user_id:
            return
        if not channel or is_direct_message_channel(channel):
            return
        if self._channels.is_registered(channel):
            return
        if user and self._policy.principal_for(channel, user).trust is TrustLevel.OWNER:
            return
        if not self._notice_log.due(channel):
            log.info("미등록 채널 %s 시도(%s). 최근에 소유자를 불러 기록만 남긴다", channel, kind)
            return
        if kind == "invite":
            body = self._notices.render(
                NoticeKey.UNREGISTERED_INVITE,
                owner=self._owner_user_id,
                bot=self._bot_name,
                who=f"<@{user}>님이" if user else "누군가",
            )
        else:
            body = self._notices.render(
                NoticeKey.UNREGISTERED_MENTION,
                owner=self._owner_user_id,
                bot=self._bot_name,
                user=user,
            )
        try:
            self._reply(channel, thread_ts, body)
            log.info("미등록 채널 %s 에서 소유자를 불렀다 (%s)", channel, kind)
        except Exception as exc:  # noqa: BLE001 - a failed notice must not break event handling
            log.warning("미등록 채널 %s 에 안내를 올리지 못했다 : %s", channel, exc)
        if self._notify_owner is None:
            return
        text = self._notices.render(
            NoticeKey.UNREGISTERED_OWNER_DM,
            who=f"<@{user}>님이" if user else "누군가",
            channel=channel,
            name=self._channel_name(channel),
            bot=self._bot_name,
            action="초대했습니다" if kind == "invite" else "불렀습니다",
        )
        try:
            self._notify_owner(text)
        except Exception as exc:  # noqa: BLE001 - a failed DM must not break event handling
            log.warning("소유자에게 미등록 채널 알림을 보내지 못했다 : %s", exc)
