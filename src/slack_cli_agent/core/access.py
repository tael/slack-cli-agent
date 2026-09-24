"""Whether an incoming request is answered at all, and the owner's automatic
channel registration that goes with it.

Both come from the original's single entry point: `is_allowed` (bot.py:3695)
called at bot.py:4969, and the registration at bot.py:4996 that runs after
admin dispatch. They ship together on purpose -- the check alone would silence
every unregistered channel with no way back in.

The judgment itself is `AccessPolicy.accepts_request`; this class only pairs it
with the registration and the notice, which need Slack.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from ..auth.policy import AccessPolicy
from ..auth.principal import TrustLevel
from ..config.channel import ChannelRegistry
from ..observability.notices import NoticeCatalog, NoticeKey

log = logging.getLogger(__name__)

# (channel, thread_ts, body)
ReplyCallback = Callable[[str, str, str], None]
ChannelName = Callable[[str], str]


class RequestAccess:
    def __init__(
        self,
        policy: AccessPolicy,
        channels: ChannelRegistry,
        channel_name: ChannelName,
        notices: NoticeCatalog,
        reply: ReplyCallback,
    ) -> None:
        self._policy = policy
        self._channels = channels
        self._channel_name = channel_name
        self._notices = notices
        self._reply = reply

    def allows(self, channel: str, user: str) -> bool:
        """A refusal is silent. Saying anything would tell someone who does
        not know this bot that it is here, and the original says nothing
        either.
        """
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
