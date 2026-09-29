"""봇 선택줄이 쓰는 한 줄 상태 명부.

지표 수집기는 봇 하나만 본다. 선택줄은 모든 봇을 봐야 하는데, 그전에는
`/api/state/<봇>` 응답의 ``bots`` 에 선택된 봇 하나만 들어가서 콘솔이
봇 1개짜리로 보였다(2026-09-15 실측). 여러 봇을 가로로 보는 관심사를
여기로 분리한다.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from pathlib import Path

from ..config.profile import Profile
from .metrics import bot_row, read_snapshot, snapshot_stale_after


class BotRoster:
    def __init__(
        self,
        search_dirs: Sequence[Path],
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._search_dirs = [Path(p) for p in search_dirs]
        self._now = now

    def rows(self) -> list[dict[str, object]]:
        now = self._now()
        return [self._row(name, now) for name in Profile.discover(self._search_dirs)]

    def _row(self, name: str, now: float) -> dict[str, object]:
        try:
            profile = Profile.load(name, self._search_dirs)
        except Exception as exc:  # noqa: BLE001 - 한 봇의 프로필이 깨져도 나머지는 보여준다
            return {
                "name": name,
                "display_name": name,
                "engine": None,
                "available": False,
                "reason": f"프로필을 읽지 못했다: {exc}",
            }
        snapshot = read_snapshot(
            profile.paths.state_snapshot, now, snapshot_stale_after(profile)
        )
        return bot_row(profile, snapshot)
