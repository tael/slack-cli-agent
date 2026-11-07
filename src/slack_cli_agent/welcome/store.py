"""Who was welcomed where, and who still waits for one (G2).

Kept in state.db like the other machine-written state (F5), not in a JSON
file as mametchi did (welcome_state.json, welcome_requests.json). The tables
are created here with IF NOT EXISTS rather than as a numbered migration, so
this module does not fight other changes over the next schema version.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from ..storage.database import Database
from ..storage.repository import SqliteRepository

#: Slack can deliver the same join twice; a re-join after this is welcomed
#: again (mametchi bot.py:9762-9764, 600 seconds).
DUPLICATE_WINDOW_SEC = 600.0

_TABLES = (
    (
        "CREATE TABLE IF NOT EXISTS welcomes ("
        " channel TEXT NOT NULL, user TEXT NOT NULL, welcomed_at REAL NOT NULL,"
        " first_line TEXT NOT NULL DEFAULT '', PRIMARY KEY (channel, user))"
    ),
    (
        "CREATE TABLE IF NOT EXISTS welcome_requests ("
        " channel TEXT NOT NULL, user TEXT NOT NULL, requested_at REAL NOT NULL,"
        " PRIMARY KEY (channel, user))"
    ),
)


class WelcomeStore(SqliteRepository):
    def __init__(self, db: Database, *, now: Callable[[], float] = time.time) -> None:
        super().__init__(db)
        self._now = now
        for sql in _TABLES:
            self._execute(sql)

    def claim(self, channel: str, user: str) -> bool:
        """Marks the welcome before it is written, so a duplicate event that
        arrives meanwhile does not write a second one. False within the window."""
        now = self._now()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT welcomed_at FROM welcomes WHERE channel = ? AND user = ?", (channel, user)
            ).fetchone()
            if row is not None and now - float(row["welcomed_at"]) < DUPLICATE_WINDOW_SEC:
                return False
            conn.execute(
                "INSERT INTO welcomes (channel, user, welcomed_at) VALUES (?, ?, ?) "
                "ON CONFLICT(channel, user) DO UPDATE SET welcomed_at = excluded.welcomed_at",
                (channel, user, now),
            )
        return True

    def release(self, channel: str, user: str) -> None:
        """Undoes a claim whose welcome never went out. Left in place, that
        person would not be welcomed on a retry (bot.py:9785)."""
        self._execute(
            "UPDATE welcomes SET welcomed_at = 0 WHERE channel = ? AND user = ?", (channel, user)
        )

    def record(self, channel: str, user: str, first_line: str) -> None:
        self._execute(
            "UPDATE welcomes SET first_line = ? WHERE channel = ? AND user = ?",
            (first_line, channel, user),
        )

    def recent(self, channel: str, limit: int) -> list[str]:
        """Opening lines of this channel's latest welcomes, oldest first.
        Per channel: one list mixed another channel's name in (bot.py:9771)."""
        rows = self._fetch_all(
            "SELECT first_line FROM welcomes WHERE channel = ? AND first_line != '' "
            "ORDER BY welcomed_at DESC LIMIT ?",
            (channel, limit),
        )
        return [str(r["first_line"]) for r in reversed(rows)]

    def request(self, channel: str, user: str) -> None:
        self._execute(
            "INSERT OR IGNORE INTO welcome_requests (channel, user, requested_at) VALUES (?, ?, ?)",
            (channel, user, self._now()),
        )

    def take_request(self) -> tuple[str, str] | None:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT channel, user FROM welcome_requests ORDER BY requested_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "DELETE FROM welcome_requests WHERE channel = ? AND user = ?",
                (row["channel"], row["user"]),
            )
        return str(row["channel"]), str(row["user"])
