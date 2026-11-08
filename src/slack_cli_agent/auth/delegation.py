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
rows with channel, message_ts, to, by, created and expires. DelegationLedger
reads it; any failure reads as "no delegation", so the turn falls back to
read-only rather than opening anything. DelegationRecorder is the writing
side, for a profile that delegates (mametchi delegation.record): the
pipeline calls it only after an owner turn posted a reply addressed to a bot.
"""

from __future__ import annotations

import fcntl
import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: How long one handed-over message stays valid. The writer stamps `expires`
#: with the same figure; checking `created` too keeps a row with an inflated
#: expiry from living past it.
TTL_SEC = 1800
#: mametchi cuts the stored scope (the posted body) at the same length.
SCOPE_MAX = 500


class DelegationLedger:
    def __init__(
        self,
        path: Path,
        *,
        self_user_id: Callable[[], str],
        own_name: str,
        delegator: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._self_user_id = self_user_id
        self._own_name = own_name
        #: The only `by` accepted, as mametchi lookup accepts only
        #: by == DELEGATOR. Another bot's row in the same file is not ours.
        self._delegator = delegator
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
        # A row this bot would have written itself is not a delegation to it:
        # a delegated turn must not be able to extend its own clearance.
        if not self._delegator or self._delegator == self._own_name:
            return False
        if row.get("by") != self._delegator:
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


class DelegationRecorder:
    """Writes one row per posted message ts that handed work to another bot.

    Same rows as mametchi delegation.record: expired rows are pruned on each
    write, the file is replaced atomically under an exclusive lock on a
    sibling `.lock`, so this and a mametchi process can share one file.
    `delegator` is this profile's name; the receiving bot accepts only rows
    whose `by` matches its own delegation_by.
    """

    def __init__(self, path: Path, *, delegator: str, clock: Callable[[], float] = time.time) -> None:
        self._path = path
        self._delegator = delegator
        self._clock = clock

    def record(
        self,
        channel: str,
        thread_ts: str,
        origin_ts: str,
        message_ts: Sequence[str],
        to_user: str,
        scope: str = "",
        kind: str = "",
    ) -> int:
        """Appends the rows and returns how many were written.

        `kind="reply"` marks a row a delegated turn wrote for its result
        message back to the delegator (2026-11-08 김태일 "마메치 턴도 권한이
        있어야해"). The delegator opens execution tools only for that one
        message and only after matching `origin_ts` against its own ledger.
        """
        if not (self._delegator and channel and message_ts and to_user):
            return 0
        now = self._clock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path.with_suffix(".lock"), "w", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            rows = [r for r in self._load() if _expires(r) > now]
            for ts in message_ts:
                rows.append({
                    "channel": channel,
                    "thread_ts": thread_ts,
                    "origin_ts": origin_ts,
                    "message_ts": ts,
                    "to": to_user,
                    "by": self._delegator,
                    "scope": (scope or "")[:SCOPE_MAX],
                    **({"kind": kind} if kind else {}),
                    "created": now,
                    "expires": now + TTL_SEC,
                })
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self._path)
        return len(message_ts)

    def _load(self) -> list[Mapping[str, Any]]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        return [row for row in data if isinstance(row, Mapping)]


def _expires(row: Mapping[str, Any]) -> float:
    try:
        return float(row.get("expires", 0))
    except (TypeError, ValueError):
        return 0.0
