"""Resolves a Slack user ID to a human-readable display name.

Lookup failures are cached too, as an empty string — the original's
choice not to keep retrying a user that already failed.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

#: How long a failed users.info lookup stays un-retried for that user.
DEFAULT_BOT_RETRY_INTERVAL_SEC = 60.0


class DisplayNameResolver:
    def __init__(self, client: Any) -> None:
        self._client = client
        self._cache: dict[str, str] = {}
        self._name_to_id: dict[str, str] = {}

    def resolve(self, user_id: str) -> str:
        if not user_id:
            return ""
        if user_id not in self._cache:
            self._cache[user_id] = self._lookup(user_id)
        name = self._cache[user_id]
        if name:
            self._name_to_id.setdefault(name, user_id)
            # If the name has a team-name suffix ("Jamie Oh Dev Team"),
            # also index the first token alone.
            head = name.split()[0]
            if head and head != name:
                self._name_to_id.setdefault(head, user_id)
        return name

    def _lookup(self, user_id: str) -> str:
        try:
            info = self._client.users_info(user=user_id)
            profile = info["user"].get("profile") or {}
            return (
                profile.get("real_name")
                or profile.get("display_name")
                or info["user"].get("name")
                or ""
            )
        except Exception:  # noqa: BLE001 - a failed lookup falls back to an empty string, not a raised error
            return ""

    def __call__(self, user_id: str) -> str:
        return self.resolve(user_id)

    def name_table(self) -> dict[str, str]:
        return dict(self._name_to_id)

    def register(self, name: str, user_id: str) -> None:
        self._name_to_id.setdefault(name, user_id)


class BotUserResolver:
    """Whether a Slack user ID belongs to a bot. Verdicts are cached.

    A failed lookup counts as a person and is not cached as a verdict, only
    throttled — the same shape SlackBotIdentity uses. Both ways of being
    wrong here have a cost: reading a failure as a bot erases a mention meant
    for a person, and caching it as a person for the life of the process
    leaves that bot's mentions in place forever, which is the loop this was
    built to stop. A retry interval keeps the wrong answer temporary without
    turning an outage into a call storm.

    The owner-only channel audit takes the opposite default for a different
    reason: reading a failure as a person there raises a violation alert with
    nothing to fix.
    """

    def __init__(
        self,
        client: Any,
        *,
        clock: Callable[[], float] = time.monotonic,
        retry_interval_sec: float = DEFAULT_BOT_RETRY_INTERVAL_SEC,
    ) -> None:
        self._client = client
        self._clock = clock
        self._retry_interval_sec = retry_interval_sec
        self._cache: dict[str, bool] = {}
        self._failed_at: dict[str, float] = {}

    def is_bot(self, user_id: str) -> bool:
        if not user_id:
            return False
        if user_id in self._cache:
            return self._cache[user_id]
        last = self._failed_at.get(user_id)
        if last is not None and self._clock() - last < self._retry_interval_sec:
            return False
        verdict = self._lookup(user_id)
        if verdict is None:
            self._failed_at[user_id] = self._clock()
            return False
        self._cache[user_id] = verdict
        return verdict

    def _lookup(self, user_id: str) -> bool | None:
        """None means the lookup itself failed, which is not a verdict."""
        try:
            user = self._client.users_info(user=user_id).get("user") or {}
        except Exception as exc:  # noqa: BLE001 - a failed lookup is retried later, not cached
            log.warning("사용자 조회 실패, 사람으로 본다 : %s : %s", user_id, exc)
            return None
        return bool(user.get("is_bot") or user.get("id") == "USLACKBOT")
