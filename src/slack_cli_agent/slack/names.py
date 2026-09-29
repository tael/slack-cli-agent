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

#: How long a failed usergroups.list call stays un-retried.
DEFAULT_GROUP_RETRY_INTERVAL_SEC = 300.0


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


class UserGroupNameResolver:
    """Slack user group ID to its handle.

    One usergroups.list call fills the whole table; groups change rarely
    (bot.py:2695). A miss is what triggers a refresh -- a group created after
    startup would otherwise stay unresolved until a restart. The refresh is
    rate-limited to retry_interval_sec, so a token without usergroups:read,
    or a mention of a group that does not exist, does not call Slack once per
    mention.
    """

    def __init__(
        self,
        client: Any,
        retry_interval_sec: float = DEFAULT_GROUP_RETRY_INTERVAL_SEC,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._cache: dict[str, str] = {}
        self._retry_interval_sec = retry_interval_sec
        self._clock = clock
        self._last_read_at: float | None = None

    def resolve(self, group_id: str) -> str:
        if not group_id:
            return ""
        if group_id not in self._cache and self._may_read():
            self._load()
        return self._cache.get(group_id, "")

    def _may_read(self) -> bool:
        if self._last_read_at is None:
            return True
        return self._clock() - self._last_read_at >= self._retry_interval_sec

    def _load(self) -> None:
        self._last_read_at = self._clock()
        try:
            answer = self._client.usergroups_list() or {}
            # A WebClient raises on ok=False, but a stub or a transport that
            # returns the body verbatim does not. Treating that as an empty
            # list would cache nothing and mark the table loaded, so a scope
            # added later would never take effect.
            if not answer.get("ok", True):
                raise RuntimeError(str(answer.get("error") or "ok=False"))
            groups = answer.get("usergroups") or []
        except Exception as exc:  # noqa: BLE001 - a missing scope must not fail the request
            log.warning("사용자 그룹 조회 실패 : %s", exc)
            return
        for group in groups:
            group_id = group.get("id")
            if group_id:
                self._cache[group_id] = group.get("handle") or group.get("name") or ""

    def __call__(self, group_id: str) -> str:
        return self.resolve(group_id)


class UserNamer:
    """What to call a user id, decided in one place.

    The speaker label used profile.display_name for this bot while the
    mention markup went through users.info, so the same bot appeared under
    two names in one transcript (sca-inw8). The original had no such gap --
    bot.py:2710 name_of_user held the judgment and both the speaker label
    and readable_mentions went through it. It could do that with constants;
    here the names come from the profile, so the judgment is shared as an
    object instead.

    The bot's own id is read per call rather than at construction: the
    lookup goes to Slack, and resolving it during assembly would make
    wiring alone call out (core/application.py).
    """

    def __init__(
        self,
        name_resolver: Callable[[str], str],
        *,
        identity: Any = None,
        bot_display_name: str = "",
        owner_user_id: str = "",
        owner_display_name: str = "",
    ) -> None:
        self._name_resolver = name_resolver
        self._identity = identity
        self._bot_display_name = bot_display_name
        self._owner_user_id = owner_user_id
        self._owner_display_name = owner_display_name

    @property
    def bot_display_name(self) -> str:
        return self._bot_display_name

    def name_of(self, user_id: str) -> str:
        if user_id:
            if self._bot_display_name and user_id == self._bot_user_id():
                return self._bot_display_name
            if self._owner_display_name and user_id == self._owner_user_id:
                return self._owner_display_name
        return self._name_resolver(user_id)

    def __call__(self, user_id: str) -> str:
        return self.name_of(user_id)

    def _bot_user_id(self) -> str:
        try:
            return str(getattr(self._identity, "user_id", "") or "")
        except Exception:  # noqa: BLE001 - identity is a Slack lookup; a failure falls back to the resolver
            return ""
