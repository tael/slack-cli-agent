"""This bot's own Slack identity — the single source of truth for
telling its own messages apart from other bots'.

Other Slack bots answer in the same channels. Treating any `bot_id` as
this bot's own would count another bot's replies as this bot's, and
recovery would then skip real unanswered mentions — the bug the
original bot.py's is_self() was fixed for, on 2026-09-02.

This lives in one class rather than being duplicated per consumer.
During the rewrite, the same _is_self logic got implemented separately
in Application, TranscriptBuilder, and EventListener, and wiring only
passed bot_id to one of them. The other two defaulted to an empty
string, silently fell back to the old broken behavior, and their unit
tests passed the whole time.
"""

from __future__ import annotations

import logging
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_RETRY_INTERVAL_SEC = 60.0


class BotIdentity(ABC):
    """Contract for this bot's identity. Callers don't know how it's looked up."""

    @property
    @abstractmethod
    def user_id(self) -> str: ...

    @property
    @abstractmethod
    def bot_id(self) -> str: ...

    @property
    @abstractmethod
    def team_id(self) -> str:
        """이 봇이 붙은 워크스페이스. chat.startStream 이 수신자 팀으로 요구한다."""

    @property
    @abstractmethod
    def known(self) -> bool: ...

    @abstractmethod
    def is_self(self, msg: Mapping[str, Any]) -> bool: ...

    @abstractmethod
    def is_mentioned(self, text: str) -> bool: ...


class SlackBotIdentity(BotIdentity):
    """Looks up identity via auth_test and caches it once resolved.

    Failures are not cached — giving up after one failure would let a
    transient Slack outage misclassify messages for the rest of the
    process's life. Retries are throttled instead, so an ongoing
    outage doesn't turn into a call storm.
    """

    def __init__(
        self,
        client: Any,
        *,
        clock: Callable[[], float] = time.monotonic,
        retry_interval_sec: float = DEFAULT_RETRY_INTERVAL_SEC,
    ) -> None:
        self._client = client
        self._clock = clock
        self._retry_interval_sec = retry_interval_sec
        self._user_id = ""
        self._bot_id = ""
        self._team_id = ""
        self._attempted_at: float | None = None

    @property
    def user_id(self) -> str:
        self._load()
        return self._user_id

    @property
    def bot_id(self) -> str:
        self._load()
        return self._bot_id

    @property
    def team_id(self) -> str:
        self._load()
        return self._team_id

    @property
    def known(self) -> bool:
        self._load()
        return self._has_identity()

    def is_self(self, msg: Mapping[str, Any]) -> bool:
        """Whether this bot posted that message.

        Returns False if identity isn't known yet — the original fell
        back to bool(msg.get("bot_id")), the exact behavior that
        caused the 2026-09-02 bug. Between the two ways to be wrong,
        this leans toward the one with a safety net: misreading this
        bot's own message as someone else's still gets caught by the
        jobs table's (channel, message_ts) uniqueness constraint when
        recovery tries to re-register it, while the other direction
        has nothing to catch it and just loses an unanswered mention.
        """
        if not self.known:
            return False
        if self._bot_id and msg.get("bot_id"):
            return bool(msg.get("bot_id") == self._bot_id)
        if self._user_id and msg.get("user"):
            return bool(msg.get("user") == self._user_id)
        return False

    def is_mentioned(self, text: str) -> bool:
        """Whether the text mentions this bot.

        Slack sometimes sends mentions as `<@U123|display name>`; a
        plain substring check misses that form and also false-matches
        `<@U_BOT2>` against `U_BOT`. Returns False if identity isn't
        known yet, same reasoning as is_self.
        """
        user_id = self.user_id
        if not user_id:
            return False
        return bool(re.search(rf"<@{re.escape(user_id)}(?:\|[^>]*)?>", text))

    def _has_identity(self) -> bool:
        """Checks the cached value without triggering a lookup.

        A successful call that returned empty strings still doesn't
        count — nothing can be matched against an empty value.
        """
        return bool(self._bot_id or self._user_id)

    def _load(self) -> None:
        if self._has_identity():
            return
        now = self._clock()
        if self._attempted_at is not None and now - self._attempted_at < self._retry_interval_sec:
            return
        self._attempted_at = now
        try:
            info = self._client.auth_test() or {}
        except Exception:  # noqa: BLE001 - a failed lookup here shouldn't block everything downstream
            log.warning("봇 신원을 조회하지 못했다")
            info = {}
        self._user_id = str(info.get("user_id") or "")
        self._bot_id = str(info.get("bot_id") or "")
        self._team_id = str(info.get("team_id") or "")
