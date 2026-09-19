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

import contextlib
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..session.manager import AUTH_FAILURE_REASON, USAGE_LIMIT_REASON


class EngineSwitcher:
    # How often to re-probe whether the primary has recovered. Same as the original ENGINE_PROBE_SEC.
    DEFAULT_PROBE_INTERVAL_SEC = 600.0
    # 인증 실패는 시간이 지나도 안 풀린다. 사람이 다시 로그인해야 풀리고,
    # 확인 자체가 실제 요청이라 주기마다 사람 하나가 1차 실패를 기다린 뒤
    # 2차로 간다. 한도와 같은 주기를 쓰면 그 대기가 10분마다 생긴다.
    AUTH_PROBE_INTERVAL_SEC = 3600.0
    #: 전환 계기. 상태 파일의 reason 값이자 실패 기록에 쓰는 이름이다.
    #: 세션 재시도 판정이 같은 문자열을 보므로 정의를 한 자리에 둔다.
    USAGE_LIMIT = USAGE_LIMIT_REASON
    AUTH_FAILURE = AUTH_FAILURE_REASON

    def __init__(self, state_path: Path,
               probe_interval_sec: float = DEFAULT_PROBE_INTERVAL_SEC,
               auth_probe_interval_sec: float = AUTH_PROBE_INTERVAL_SEC) -> None:
        self._path = state_path
        self._probe_interval_sec = probe_interval_sec
        self._auth_probe_interval_sec = auth_probe_interval_sec

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
        with contextlib.suppress(OSError):
            self._path.unlink()

    def is_switched(self) -> bool:
        return bool(self.load().get("engine"))

    def is_approved(self) -> bool:
        return self.load().get("approval") == "approved"

    def begin_switch(self, detail: str, *, engine_name: str = "",
                     reason: str = USAGE_LIMIT,
                     probe_ok: bool = False, probe_detail: str = "") -> dict[str, Any]:
        """Switches immediately; the bot only replies with a notice until approved."""
        state = {
            "engine": engine_name,
            "reason": reason,
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

    def reason(self) -> str:
        return str(self.load().get("reason") or self.USAGE_LIMIT)

    def should_probe(self, now: float) -> bool:
        state = self.load()
        if not state:
            return False
        interval = (
            self._auth_probe_interval_sec
            if state.get("reason") == self.AUTH_FAILURE
            else self._probe_interval_sec
        )
        last_probe_at = float(state.get("last_probe_at", 0))
        return (now - last_probe_at) > interval

    def mark_probed(self, now: float) -> None:
        state = self.load()
        if not state:
            return
        state["last_probe_at"] = now
        self.save(state)

    def limit_reply(self) -> str:
        """What to tell a human while approval is pending or denied.

        계기마다 사람이 할 일이 다르다. 한도는 기다리면 풀리고 인증 실패는
        다시 로그인해야 풀린다. 같은 문구로 내면 로그인이 풀린 것을 한도로
        읽어 아무도 손대지 않는다.
        """
        state = self.load()
        detail = (state.get("detail") or "").strip()
        if state.get("reason") == self.AUTH_FAILURE:
            body = "실행기 로그인이 풀려 지금은 답할 수 없어요."
        else:
            body = "구독 사용 한도에 걸려 지금은 답할 수 없어요."
        if detail:
            body += f"\n\n> {detail}"
        return body
