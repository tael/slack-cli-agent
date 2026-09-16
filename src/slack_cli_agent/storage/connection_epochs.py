"""SQLite adapter for the connection-epoch ledger.

Writer (ingress) and claimer (worker) are separate processes, so this has to
be on disk rather than in memory. Each profile has its own state_dir, so each
bot gets its own ledger.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from ..reliability.connection import (
    CatchupTriggerStore,
    ClaimedGap,
    ConnectionEpoch,
    ConnectionEpochRecorder,
    ConnectionKind,
)
from .database import Database
from .repository import SqliteRepository

# Writing every poll would mean a write every 2 seconds. The gap estimate is
# off by at most this interval, which the catch-up grace absorbs.
DEFAULT_HEARTBEAT_SEC = 15.0

PENDING = "PENDING"
RUNNING = "RUNNING"
DONE = "DONE"


class SqliteConnectionEpochs(SqliteRepository, ConnectionEpochRecorder, CatchupTriggerStore):
    def __init__(
        self,
        db: Database,
        *,
        now: Callable[[], float] = time.time,
        heartbeat_sec: float = DEFAULT_HEARTBEAT_SEC,
    ) -> None:
        super().__init__(db)
        self._now = now
        self._heartbeat_sec = heartbeat_sec
        self._last_written = 0.0

    # Ingress side ----------------------------------------------------------

    def record_connection(self, kind: ConnectionKind) -> ConnectionEpoch:
        now = self._now()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT last_seen_at FROM connection_epochs ORDER BY generation DESC LIMIT 1"
            ).fetchone()
            # No prior epoch means no known gap; using the connect time keeps
            # the default window, which is right for a first boot.
            gap_started_at = float(row["last_seen_at"]) if row is not None else now
            cur = conn.execute(
                "INSERT INTO connection_epochs"
                " (kind, connected_at, gap_started_at, last_seen_at, state)"
                " VALUES (?, ?, ?, ?, ?)",
                (kind.value, now, gap_started_at, now, PENDING),
            )
            generation = int(cur.lastrowid or 0)
        self._last_written = now
        return ConnectionEpoch(
            generation=generation,
            kind=kind,
            connected_at=now,
            gap_started_at=gap_started_at,
        )

    def note_alive(self) -> None:
        now = self._now()
        if now - self._last_written < self._heartbeat_sec:
            return
        self._last_written = now
        self._execute(
            "UPDATE connection_epochs SET last_seen_at = ?"
            " WHERE generation = (SELECT MAX(generation) FROM connection_epochs)",
            (now,),
        )

    def reconnect_timestamps(self) -> tuple[float, ...]:
        """Connect times of reconnect epochs, oldest first.

        Read by the state snapshot, which the worker writes: the socket lives in
        ingress, so counting from this process's own logs is always zero
        (sca-qi5.3).
        """
        rows = self._fetch_all(
            "SELECT connected_at FROM connection_epochs WHERE kind = ? ORDER BY generation",
            (ConnectionKind.RECONNECT.value,),
        )
        return tuple(float(r["connected_at"]) for r in rows)

    # Worker side -----------------------------------------------------------

    def pending(self) -> list[ConnectionEpoch]:
        rows = self._fetch_all(
            "SELECT generation, kind, connected_at, gap_started_at FROM connection_epochs"
            " WHERE state != ? ORDER BY generation",
            (DONE,),
        )
        return [
            ConnectionEpoch(
                generation=int(r["generation"]),
                kind=ConnectionKind(r["kind"]),
                connected_at=float(r["connected_at"]),
                gap_started_at=float(r["gap_started_at"]),
            )
            for r in rows
        ]

    def claim(self, owner: str, lease_sec: float) -> ClaimedGap | None:
        now = self._now()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT generation, connected_at, gap_started_at FROM connection_epochs"
                " WHERE state = ? OR (state = ? AND lease_until <= ?)"
                " ORDER BY generation",
                (PENDING, RUNNING, now),
            ).fetchall()
            if not rows:
                return None
            generations = tuple(int(r["generation"]) for r in rows)
            placeholders = ",".join("?" for _ in generations)
            conn.execute(
                f"UPDATE connection_epochs SET state = ?, lease_owner = ?, lease_until = ?,"
                f" attempts = attempts + 1 WHERE generation IN ({placeholders})",
                (RUNNING, owner, now + lease_sec, *generations),
            )
        return ClaimedGap(
            generations=generations,
            gap_started_at=min(float(r["gap_started_at"]) for r in rows),
            connected_at=max(float(r["connected_at"]) for r in rows),
        )

    def complete(self, generations: tuple[int, ...]) -> None:
        if not generations:
            return
        placeholders = ",".join("?" for _ in generations)
        self._execute(
            f"UPDATE connection_epochs SET state = ?, completed_at = ?, lease_until = NULL"
            f" WHERE generation IN ({placeholders})",
            (DONE, self._now(), *generations),
        )

    def release(self, generations: tuple[int, ...]) -> None:
        if not generations:
            return
        placeholders = ",".join("?" for _ in generations)
        self._execute(
            f"UPDATE connection_epochs SET state = ?, lease_owner = '', lease_until = NULL"
            f" WHERE generation IN ({placeholders})",
            (PENDING, *generations),
        )

    def purge_done(self, older_than: float) -> int:
        """Drops closed epochs so the ledger doesn't grow without bound."""
        cur = self._execute(
            "DELETE FROM connection_epochs WHERE state = ? AND completed_at < ?",
            (DONE, older_than),
        )
        return cur.rowcount or 0
