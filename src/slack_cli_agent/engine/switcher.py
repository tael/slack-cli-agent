"""Engine switch state machine.

engine_state.json is a plain file, not a database — a human sometimes
opens it directly to check or roll back state (path:
StatePaths.engine_state).

Switching happens immediately; the bot only replies with a limit
notice until a human approves it. Changing which engine answers is a
decision about how answers get made, so a human makes it — same as
the original's 2026-09-11 decision.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class EngineSwitcher:
    # How often to re-probe whether the primary has recovered. Same as the original ENGINE_PROBE_SEC.
    DEFAULT_PROBE_INTERVAL_SEC = 600.0

    def __init__(self, state_path: Path,
               probe_interval_sec: float = DEFAULT_PROBE_INTERVAL_SEC) -> None:
        self._path = state_path
        self._probe_interval_sec = probe_interval_sec

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, state: Mapping[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(dict(state), ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )

    def clear(self) -> None:
        try:
            self._path.unlink()
        except OSError:
            pass

    def is_switched(self) -> bool:
        return bool(self.load().get("engine"))

    def is_approved(self) -> bool:
        return self.load().get("approval") == "approved"

    def begin_switch(self, detail: str, *, engine_name: str = "",
                     probe_ok: bool = False, probe_detail: str = "") -> dict[str, Any]:
        """Switches immediately; the bot only replies with a limit notice until approved."""
        state = {
            "engine": engine_name,
            "reason": "usage_limit",
            "detail": detail,
            "switched_at": time.time(),
            "approval": "pending",
            "last_probe_at": time.time(),
            "probe_ok": probe_ok,
            "probe_detail": probe_detail,
        }
        self.save(state)
        return state

    def approve(self) -> None:
        state = self.load()
        state["approval"] = "approved"
        self.save(state)

    def deny(self) -> None:
        state = self.load()
        state["approval"] = "denied"
        self.save(state)

    def recover(self) -> None:
        """Primary has recovered — clears state so the next request goes back to primary."""
        self.clear()

    def should_probe(self, now: float) -> bool:
        state = self.load()
        if not state:
            return False
        last_probe_at = float(state.get("last_probe_at", 0))
        return (now - last_probe_at) > self._probe_interval_sec

    def mark_probed(self, now: float) -> None:
        state = self.load()
        if not state:
            return
        state["last_probe_at"] = now
        self.save(state)

    def limit_reply(self) -> str:
        """What to tell a human while approval is pending or denied."""
        detail = (self.load().get("detail") or "").strip()
        body = "구독 사용 한도에 걸려 지금은 답할 수 없어요."
        if detail:
            body += f"\n\n> {detail}"
        return body
