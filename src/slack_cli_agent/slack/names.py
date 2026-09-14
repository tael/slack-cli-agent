"""Resolves a Slack user ID to a human-readable display name.

Lookup failures are cached too, as an empty string — the original's
choice not to keep retrying a user that already failed.
"""

from __future__ import annotations

from typing import Any


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
