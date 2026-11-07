"""Reads the delegation ledger another bot keeps when it hands work to this one.

controller_user_ids only says which bot accounts may file a request. Whether
a given request carries execution tools is decided here: the delegating bot
writes the ts of each message it posted to hand work over, in its own turn
for the owner, into a local file only it writes. A Slack message alone opens
nothing -- posting the same text again leaves no row (mametchi delegation.py,
2026-10-22). Without that, a bot talking to this one was enough to open the
execution path, which is why mametchi took its peer back out of operators on
2026-09-21.

The file format is mametchi's ~/.mametchi/delegations.json: a JSON list of
rows with channel, message_ts, to, by, created and expires. This side only
reads it. Any failure reads as "no delegation", so the turn falls back to
read-only rather than opening anything.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: How long one handed-over message stays valid. The writer stamps `expires`
#: with the same figure; checking `created` too keeps a row with an inflated
#: expiry from living past it.
TTL_SEC = 1800


class DelegationLedger:
    def __init__(
        self,
        path: Path,
        *,
        self_user_id: Callable[[], str],
        own_name: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._self_user_id = self_user_id
        self._own_name = own_name
        self._clock = clock

    def lookup(self, channel: str, message_ts: str) -> Mapping[str, Any] | None:
        """The live row that handed this message to this bot, or None."""
        me = self._self_user_id()
        if not (channel and message_ts and me):
            return None
        now = self._clock()
        for row in self._rows():
            if self._matches(row, channel, message_ts, me, now):
                return row
        return None

    def _matches(
        self, row: Mapping[str, Any], channel: str, message_ts: str, me: str, now: float
    ) -> bool:
        by = row.get("by")
        # A row this bot would have written itself is not a delegation to it:
        # a delegated turn must not be able to extend its own clearance.
        if not isinstance(by, str) or not by or by == self._own_name:
            return False
        if row.get("channel") != channel or row.get("message_ts") != message_ts:
            return False
        if row.get("to") != me:
            return False
        try:
            created = float(row.get("created", 0))
            expires = float(row.get("expires", 0))
        except (TypeError, ValueError):
            return False
        return expires > now and now - created <= TTL_SEC

    def _rows(self) -> list[Mapping[str, Any]]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            log.warning("위임 원장을 읽지 못해 위임 없음으로 본다 : %s : %s", self._path, exc)
            return []
        if not isinstance(data, list):
            return []
        return [row for row in data if isinstance(row, Mapping)]
