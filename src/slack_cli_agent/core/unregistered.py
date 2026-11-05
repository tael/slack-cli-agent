"""When the owner was last called in each unregistered channel.

Ported from the original's `record_unregistered_attempt` (bot.py, 2026-11-05).
Kept in a file so a restart does not call the owner again in the same channel.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

NOTICE_COOLDOWN_SEC = 6 * 3600


class UnregisteredNoticeLog:
    def __init__(
        self,
        path: Path,
        cooldown_sec: float = NOTICE_COOLDOWN_SEC,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._cooldown = cooldown_sec
        self._clock = clock
        self._lock = threading.Lock()

    def due(self, channel: str) -> bool:
        """True if the owner may be called in this channel now; the call is
        recorded in the same step so two events racing here call once."""
        now = self._clock()
        with self._lock:
            notified = self._load()
            if now - float(notified.get(channel, 0)) < self._cooldown:
                return False
            notified[channel] = now
            self._save(notified)
        return True

    def _load(self) -> dict[str, float]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("미등록 채널 기록을 읽지 못해 빈 기록으로 본다 : %s", exc)
            return {}
        notified = data.get("notified") if isinstance(data, dict) else None
        return dict(notified) if isinstance(notified, dict) else {}

    def _save(self, notified: dict[str, float]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps({"notified": notified}, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as exc:
            log.warning("미등록 채널 기록을 남기지 못했다 : %s", exc)
