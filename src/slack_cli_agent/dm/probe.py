"""Probe detection for a non-owner DM (E4, mametchi bot.py:7209 dm_probe).

A word rule, not the model: on 2026-10-14 sonnet read "배송 앱이 튕겨요" and
"적재 지연 확인" as probes, and warnings piling up on false hits teach the
owner to ignore them.
"""

from __future__ import annotations

import re
import threading

#: mametchi's table without its bot name (마메치 의 권한/토큰), which is profile-specific.
PROBE_PATTERN = re.compile(
    r"(시스템\s*프롬프트|프롬프트|지침|system\s*prompt|instruction|모델\s*(이|명|뭐|버전)|"
    r"도구\s*목록|어떤\s*도구|(네|너)\s*(의\s*)?(권한|토큰)|api\s*키|비밀번호|자격\s*증명|credential|"
    r"설정\s*파일|claude\.md|agents\.md|무시하(고|라|세요)|ignore|jailbreak|탈옥|관리자가\s*허락)",
    re.IGNORECASE,
)


def is_probe(text: str | None) -> bool:
    return bool(PROBE_PATTERN.search(text or ""))


class DailyCounter:
    """Per-person counts for one day. In memory, as in mametchi: a restart
    starts the day over, which only lets a few more replies through."""

    def __init__(self) -> None:
        self._counts: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def add(self, user: str, day: str) -> int:
        with self._lock:
            # Older days are never read again; dropping them keeps a process
            # running for weeks from growing this without bound.
            for key in [k for k in self._counts if k[1] != day]:
                del self._counts[key]
            key = (user, day)
            self._counts[key] = self._counts.get(key, 0) + 1
            return self._counts[key]
