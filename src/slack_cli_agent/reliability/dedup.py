"""Catches Slack redelivering the same event (reconnect, retry).

This sits in front of the queue's own `UNIQUE(channel, message_ts)`, which
only prevents double-enqueueing — this catches the duplicate before it gets
that far. Entries are kept in memory and cleared wholesale past `max_entries`;
not a proper LRU, but the cap comfortably covers real-world redelivery
windows (seconds to minutes).
"""

from __future__ import annotations

import threading


class DeduplicationTracker:
    def __init__(self, max_entries: int = 2000) -> None:
        self._seen: set[tuple[str, str]] = set()
        self._max = max_entries
        self._lock = threading.Lock()

    def already_seen_event(self, channel: str, ts: str) -> bool:
        key = (channel, ts)
        with self._lock:
            if key in self._seen:
                return True
            if len(self._seen) >= self._max:
                self._seen.clear()
            self._seen.add(key)
            return False

    def forget_event(self, channel: str, ts: str) -> None:
        """Undoes the record so Slack's redelivery is allowed through.

        Slack ACKs the socket event before the handler runs, so a request the
        ingress failed to store is only recoverable if the redelivery isn't
        suppressed here (sca-if6).
        """
        with self._lock:
            self._seen.discard((channel, ts))
