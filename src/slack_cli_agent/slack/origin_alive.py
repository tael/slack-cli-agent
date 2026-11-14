"""Checks whether the question message is still in its thread before replying.

While a reply is being produced the asker can delete the question and
repost it elsewhere; the coordinates captured at intake then point at a
different thread, and posting there drops the answer onto strangers'
messages (bot.py:4540 origin_alive, after a dispatch-policy answer landed
in a settlement thread the finance team was sitting in, 2026-09-24).

A deleted message comes back as an error. In the 2026-09-24 measurement a
deleted reply returned ``thread_not_found`` and a live one returned
itself. Only those two error codes are read as "gone"; every other
failure, and a call that succeeds but doesn't list the ts, stays unknown
so a lookup hiccup never discards a good answer.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class OriginAliveChecker(Protocol):
    """The survival check the pipeline depends on, kept Slack-agnostic so a
    test can supply a stub and a different transport its own implementation.
    """

    def alive(self, channel: str, ts: str) -> bool | None: ...

#: How many replies to pull back when confirming the question is still there.
#: The question is usually the thread parent, so a small window suffices.
ORIGIN_ALIVE_LIMIT = 50

#: The Slack error codes that mean the message is truly gone, as opposed to
#: a transient lookup failure. Kept narrow on purpose (bot.py:4566).
_DELETED_ERRORS = frozenset({"thread_not_found", "message_not_found"})


class SlackOriginAliveChecker:
    """Confirms a message's survival via conversations.replies.

    ``alive`` returns ``True`` when the message is still present, ``False``
    when Slack reports it gone, and ``None`` when that can't be told — a
    lookup error other than the deleted codes, or a success that simply
    didn't list the ts. ``None`` must never be collapsed into ``False``:
    reading a lookup failure as a deletion throws away a good answer.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def alive(self, channel: str, ts: str) -> bool | None:
        try:
            resp = self._client.conversations_replies(
                channel=channel, ts=ts, limit=ORIGIN_ALIVE_LIMIT
            )
        except Exception as exc:  # noqa: BLE001 - an unknown failure stays unknown, not a deletion
            err = _error_code(exc)
            if err in _DELETED_ERRORS:
                return False
            logger.warning("물음 생존 확인 실패 channel=%s ts=%s error=%s", channel, ts, exc)
            return None
        messages = (resp or {}).get("messages") or []
        if any(m.get("ts") == ts for m in messages):
            return True
        return None


def _error_code(exc: Exception) -> str:
    resp = getattr(exc, "response", None)
    if resp is None:
        return ""
    try:
        return resp.get("error", "") or ""
    except Exception:  # noqa: BLE001 - a malformed error payload is just "unknown"
        return ""
