"""중복 처리 방어 — 이벤트 수신 단계.

큐의 `UNIQUE(channel, message_ts)` 가 등록 단계의 중복(같은 요청을 두 번
집어넣는 것)을 막는다. 이 클래스는 그 앞 단계, 슬랙이 같은 이벤트를 두 번
보내는 경우(재연결, 재시도)를 본다.

원본 `_seen_events`, `already_seen_event` 를 그대로 옮겼다. (channel, ts) 는
메시지 하나를 유일하게 가리키므로 이미 들어온 조합이면 새 요청이 아니라 같은
말의 재전송으로 본다.

메모리에 두고 상한(2000)을 넘기면 통째로 비운다. 순서를 보장하는 자료구조가
아니라 정교한 LRU 는 아니지만, 이 정도 상한이면 실제 재전송 창(수 초에서 수
분)을 덮는 데 충분하다 — 원본 주석의 판단을 그대로 따른다.

2026-09-01 : 8초 간격 서로 다른 두 사람 메시지가 각각 답을 받았는데, 그중
한쪽이 실제로는 같은 이벤트의 재전송이었을 가능성을 배제하지 못해 추가됐다.
"""

from __future__ import annotations

import threading


class DeduplicationTracker:
    def __init__(self, max_entries: int = 2000) -> None:
        self._seen: set[tuple[str, str]] = set()
        self._max = max_entries
        self._lock = threading.Lock()

    def already_seen_event(self, channel: str, ts: str) -> bool:
        """이 (채널, ts) 조합을 이미 처리 대상으로 받은 적 있는지 본다.

        처음 보는 조합이면 바로 기록하고 False 를 돌려준다.
        """
        key = (channel, ts)
        with self._lock:
            if key in self._seen:
                return True
            if len(self._seen) >= self._max:
                self._seen.clear()
            self._seen.add(key)
            return False
