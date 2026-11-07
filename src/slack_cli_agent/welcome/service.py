"""Writes and posts a welcome for someone who joined (G2).

Ports mametchi bot.py:9653-9827. A channel turns it on with `"welcome": true`
in channels.json; the post goes to the channel body, not a thread. The
engine turn is read-only: it looks the person up, it does not act.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from ..config.channel import ChannelConfig, channel_is_rich
from ..engine.base import EngineResponse
from ..plugin.hooks import EngineSlot, SlotUnavailable
from ..prompt.sections import is_silent
from .store import WelcomeStore

log = logging.getLogger(__name__)

RECENT_KEEP = 6

#: Channel character by mode (bot.py:9682). An unlisted mode must not get
#: an invented one.
CHANNEL_INTRO = {
    "chat": "가벼운 수다와 잡담을 편하게 나누는 채널이다",
    "private": "업무 이야기를 나누는 비공개 채널이다. 잡담방이나 수다 채널로 소개하지 않는다",
}
DEFAULT_INTRO = "업무 채널이다. 채널 성격을 지어내지 않는다"


def welcome_enabled(config: ChannelConfig | None) -> bool:
    """Only a literal true turns it on, so a stray string does not."""
    return config is not None and config.extra.get("welcome") is True


def is_member_join(event: Mapping[str, Any]) -> bool:
    return event.get("subtype") == "channel_join" and bool(event.get("user"))


def example_line(body: str, channel_name: str = "") -> str:
    """The opening of a welcome, shown to the next one so it differs. The
    person's mention and the channel name are removed (bot.py:9746)."""
    text = re.sub(r"<@[A-Z0-9]+(?:\|[^>]*)?>", "", body)
    text = re.sub(r"<#[A-Z0-9]+(\|[^>]*)?>", "이 채널", text)
    if channel_name:
        text = text.replace(f"#{channel_name}", "이 채널").replace(channel_name, "이 채널")
    return text.strip().split("\n")[0][:80]


class _Channels(Protocol):
    def get(self, channel_id: str) -> ChannelConfig | None: ...


class _Publisher(Protocol):
    def post(
        self, channel: str, thread_ts: str, text: str, rich: bool, *, flat: bool = False
    ) -> str | None: ...


class WelcomeService:
    def __init__(
        self,
        *,
        store: WelcomeStore,
        client: Any,
        channels: _Channels,
        publisher: _Publisher,
        run: Callable[[str], EngineResponse],
        template: Callable[[], str],
        self_user_id: Callable[[], str],
    ) -> None:
        self.store = store
        self._client = client
        self._channels = channels
        self._publisher = publisher
        self._run = run
        self._template = template
        self._self_user_id = self_user_id

    def welcome(self, channel: str, user: str, engine: EngineSlot) -> str | None:
        """Posts one welcome; None when it was skipped or failed. A
        SlotUnavailable from `engine` propagates after the claim is undone,
        so the caller can queue a retry."""
        if not user or user == self._self_user_id():
            return None
        is_bot, profile = self._profile(user)
        if is_bot:
            return None
        if not self.store.claim(channel, user):
            log.info("방금 환영한 사람이다. 중복 이벤트로 보고 넘긴다 : %s:%s", channel, user)
            return None
        config = self._channels.get(channel)
        name = config.name if config else channel
        try:
            prompt = self._prompt(channel, user, profile, config)
            with engine():
                response = self._run(prompt)
            body = (response.body or "").strip()
            if not response.ok or not body or is_silent(body):
                log.warning("환영 글을 만들지 못했다 : %s:%s %s", channel, user, response.failure_reason)
                self.store.release(channel, user)
                return None
            posted = self._publisher.post(channel, "", body, channel_is_rich(config), flat=True)
        except BaseException:
            self.store.release(channel, user)
            raise
        if posted is None:
            log.warning("환영 글을 올리지 못했다 : %s:%s", channel, user)
            self.store.release(channel, user)
            return None
        self.store.record(channel, user, example_line(body, name))
        return body

    def drain(self, engine: EngineSlot) -> None:
        """Welcomes everyone queued by the admin command or a missed slot."""
        while (pair := self.store.take_request()) is not None:
            try:
                self.welcome(pair[0], pair[1], engine)
            except SlotUnavailable:
                # Put back so the next round retries it, then stop this round.
                self.store.request(*pair)
                raise
            except Exception:
                # Dropped like the original (bot.py:11713): retrying a broken
                # request every round would only repeat the failure.
                log.exception("소급 환영 실패 : %s", pair)

    def _profile(self, user: str) -> tuple[bool, str]:
        try:
            info = self._client.users_info(user=user)["user"]
        except Exception:  # noqa: BLE001 - a welcome without the profile still goes out
            log.warning("환영 대상 프로필 조회 실패 : %s", user)
            return False, "조회 실패"
        p = info.get("profile") or {}
        line = (f"이름 {p.get('real_name') or ''}, 직함 {p.get('title') or '없음'}, "
                f"표시명 {p.get('display_name') or ''}")
        return bool(info.get("is_bot")), line

    def _prompt(self, channel: str, user: str, profile: str, config: ChannelConfig | None) -> str:
        recent = self.store.recent(channel, RECENT_KEEP)
        slots = {
            "CHANNEL_NAME": config.name if config else channel,
            "CHANNEL_INTRO": CHANNEL_INTRO.get(config.mode if config else "", DEFAULT_INTRO),
            "USER": user,
            "PROFILE": profile,
            "RECENT": "\n---\n".join(recent) if recent else "(아직 없음)",
        }
        text = self._template()
        for key, value in slots.items():
            text = text.replace(f"<<{key}>>", value)
        return text
