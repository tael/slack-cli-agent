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
#: How long after a message was posted the ledger may still be catching up.
#: The delegator can only write the row after chat.postMessage returns the
#: ts, so the receiving bot's event can arrive first. 2026-11-08 15:36:57 the
#: delegation for #test-bw-sop was looked up at 15:36:59.8 and written at
#: 15:37:01.6; the turn ran read-only and gave up (김태일 "권한없다고 포기").
WRITE_GRACE_SEC = 20.0
#: Poll step while waiting inside the grace window.
WRITE_POLL_SEC = 0.5


class DelegationLedger:
    def __init__(
        self,
        path: Path,
        *,
        self_user_id: Callable[[], str],
        own_name: str,
        delegator: str,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        write_grace: float = WRITE_GRACE_SEC,
        reply_origin: Path | None = None,
    ) -> None:
        self._path = path
        #: Set when this bot is the delegator and accepts only replies to work
        #: it handed out (mametchi lookup_reply): the row must be kind=reply
        #: and its origin_ts a message this bot wrote, in this file, for the
        #: replying bot. None keeps the plain delegation check.
        self._reply_origin = reply_origin
        self._sleep = sleep
        self._write_grace = write_grace
        self._self_user_id = self_user_id
        self._own_name = own_name
        #: The only `by` accepted, as mametchi lookup accepts only
        #: by == DELEGATOR. Another bot's row in the same file is not ours.
        self._delegator = delegator
        self._clock = clock

    def lookup(self, channel: str, message_ts: str, from_user: str = "") -> Mapping[str, Any] | None:
        """The live row that handed this message to this bot, or None."""
        me = self._self_user_id()
        if not (channel and message_ts and me):
            return None
        now = self._clock()
        for row in self._rows():
            if self._matches(row, channel, message_ts, me, now):
                origin = self._reply_origin
                if origin is not None and not self._answers_own(origin, row, channel, from_user):
                    continue
                return row
        return None

    def _answers_own(
        self, origin: Path, row: Mapping[str, Any], channel: str, from_user: str
    ) -> bool:
        if row.get("kind") != "reply" or not row.get("origin_ts") or not from_user:
            return False
        return any(
            r.get("by") == self._own_name and r.get("channel") == channel
            and r.get("message_ts") == row["origin_ts"] and r.get("to") == from_user
            for r in _read_rows(origin)
        )

    def lookup_settled(
        self, channel: str, message_ts: str, from_user: str = ""
    ) -> Mapping[str, Any] | None:
        """lookup, but waits out the delegator's write lag for a fresh message.

        A miss on a message younger than write_grace is not final: the row
        may simply not be written yet. Poll until it shows up or the message
        is write_grace old. An older message is judged once, as before, so a
        stale or forged request costs no wait.
        """
        found = self.lookup(channel, message_ts, from_user)
        if found is not None or not (channel and message_ts and self._self_user_id()):
            return found
        try:
            posted = float(message_ts)
        except ValueError:
            return None
        deadline = posted + self._write_grace
        waited = False
        while self._clock() < deadline:
            waited = True
            self._sleep(min(WRITE_POLL_SEC, max(0.0, deadline - self._clock())))
            found = self.lookup(channel, message_ts, from_user)
            if found is not None:
                log.info("위임 원장 기록을 기다려 찾았다 : %s : %s", channel, message_ts)
                return found
        if waited:
            log.info("위임 원장 기록을 %.0f초 기다렸지만 없다 : %s : %s", self._write_grace, channel, message_ts)
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
        return _read_rows(self._path)


def _read_rows(path: Path) -> list[Mapping[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        log.warning("위임 원장을 읽지 못해 위임 없음으로 본다 : %s : %s", path, exc)
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
