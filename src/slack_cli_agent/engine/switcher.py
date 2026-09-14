"""엔진 전환 상태 기계.

``engine_state.json`` 을 파일로 둔다. 사람이 직접 열어 확인하고 되돌리는
경우가 있어 DB 가 아니라 파일로 관리한다(경로는 StatePaths.engine_state).

전환은 즉시 하고, 사람이 승인하기 전에는 한도 안내만 답한다. 실행기를 바꾸는
것은 답하는 방식을 바꾸는 일이라 사람이 정한다 — 2026-09-11 원본 결정 그대로.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class EngineSwitcher:
    # 기본 실행기가 돌아왔는지 다시 떠보는 주기. 원본 ENGINE_PROBE_SEC 과 같다.
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
        """전환은 즉시 한다. 사람이 승인하기 전에는 한도 안내만 답한다."""
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
        """기본 실행기가 돌아왔다. 상태를 지워 다음 요청부터 1차로 돌린다."""
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
        """승인 전이거나 거부 상태일 때 사람에게 내는 말."""
        detail = (self.load().get("detail") or "").strip()
        body = "구독 사용 한도에 걸려 지금은 답할 수 없어요."
        if detail:
            body += f"\n\n> {detail}"
        return body
