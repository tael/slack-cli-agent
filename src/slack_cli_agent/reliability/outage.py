"""슬랙 도달 여부의 상태 전이.

"지금 닿는가" 와 "끊겼다가 방금 돌아왔는가" 는 다른 판정이다. 뒤쪽은 앞 회차의
결과를 기억해야 나오고, 그 기억을 쓰는 곳이 둘이다 — 접수 프로세스의 소켓
재기동 판정과, 워커의 복구 직후 되짚기다. 같은 전이 판정을 양쪽이 각자 들고
있으면 한쪽만 고칠 때 동작이 갈린다.
"""

from __future__ import annotations

import time
from collections.abc import Callable


class OutageTracker:
    """닿는지를 회차마다 보고, 끊겼다 돌아온 회차에 그 끊긴 시간을 돌려준다."""

    def __init__(self, *, reachable: Callable[[], bool], now: Callable[[], float] = time.time) -> None:
        self._reachable = reachable
        self._now = now
        self._healthy = True
        self._down_since: float | None = None

    @property
    def healthy(self) -> bool:
        """마지막 회차에 닿았는가."""
        return self._healthy

    def check(self) -> float | None:
        """한 회차를 본다. 끊겼다 돌아온 회차에만 끊긴 시간(초)을 돌려준다.

        복구를 알린 다음 회차는 다시 `None` 이다. 매 회차 복구로 보면 그것을
        계기로 도는 되짚기가 쉬지 않고 돈다.
        """
        ok = self._reachable()

        if not ok:
            if self._healthy:
                self._healthy = False
                self._down_since = self._now()
            return None

        if self._healthy:
            return None

        since = self._down_since if self._down_since is not None else self._now()
        outage = self._now() - since
        self._healthy = True
        self._down_since = None
        return outage
