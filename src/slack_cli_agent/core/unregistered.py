"""When the owner was last called in each unregistered channel, and which
unregistered channels each person recently tried to use the bot in.

Ported from the original's `record_unregistered_attempt` and
`unregistered_context` (bot.py:7385-7421, 2026-11-05). Kept in a file so a
restart does not call the owner again in the same channel. The per-person
attempts let a DM reply know which channel "it doesn't answer there" means;
the original's DM reply could not tell and said the DM named no channel.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .timezones import KST

log = logging.getLogger(__name__)

NOTICE_COOLDOWN_SEC = 6 * 3600
#: How long, and how many, attempts are kept per person (bot.py:7367, 7395).
ATTEMPT_KEEP_SEC = 3 * 86400
ATTEMPT_KEEP_COUNT = 10


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
        return self.record(channel, "", "", "")

    def record(self, channel: str, user: str, kind: str, name: str) -> bool:
        """Records one attempt by `user` in `channel` and returns `due` for it.
        An empty user leaves the per-person record alone (an invite Slack did
        not attribute)."""
        now = self._clock()
        with self._lock:
            data = self._load()
            notified = data["notified"]
            if user:
                rows = [
                    r for r in data["attempts"].get(user, [])
                    if isinstance(r, dict)
                    and now - float(r.get("at", 0)) < ATTEMPT_KEEP_SEC
                    and r.get("channel") != channel
                ]
                rows.append({"channel": channel, "name": name, "kind": kind, "at": now})
                data["attempts"][user] = rows[-ATTEMPT_KEEP_COUNT:]
            due = now - float(notified.get(channel, 0)) >= self._cooldown
            if due:
                notified[channel] = now
            if due or user:
                self._save(data)
        return due

    def recent_context(self, user: str, is_registered: Callable[[str], bool]) -> str:
        """The `<recent_channels>` block a DM reply is given: unregistered
        channels this person called the bot in within the keep window. Empty
        when there are none, so the caller can prepend it unconditionally."""
        if not user:
            return ""
        now = self._clock()
        with self._lock:
            rows = self._load()["attempts"].get(user, [])
        lines = []
        for r in rows:
            if not isinstance(r, dict) or not r.get("channel"):
                continue
            if now - float(r.get("at", 0)) >= ATTEMPT_KEEP_SEC or is_registered(r["channel"]):
                continue
            when = datetime.fromtimestamp(float(r["at"]), KST).strftime("%m/%d %H:%M")
            what = "초대" if r.get("kind") == "invite" else "멘션"
            lines.append(f"- <#{r['channel']}> 이름 {r.get('name') or '확인 못 함'}, {when} {what}")
        if not lines:
            return ""
        return "<recent_channels>\n" + "\n".join(lines) + "\n</recent_channels>\n"

    def _load(self) -> dict[str, Any]:
        """The whole file, with both keys present. Keys this class does not
        know are kept so a save never drops what another writer put there."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError) as exc:
            log.warning("미등록 채널 기록을 읽지 못해 빈 기록으로 본다 : %s", exc)
            data = {}
        if not isinstance(data, dict):
            data = {}
        for key in ("notified", "attempts"):
            if not isinstance(data.get(key), dict):
                data[key] = {}
        return data

    def _save(self, data: dict[str, Any]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as exc:
            log.warning("미등록 채널 기록을 남기지 못했다 : %s", exc)
