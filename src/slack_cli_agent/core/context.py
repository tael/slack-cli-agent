"""요청 하나의 불변 맥락.

원본은 슬랙 이벤트 dict 에 _unaddressed·_late·_requeued 같은 키를 추가해 돌렸다.
어떤 키가 언제 붙는지가 코드 전체에 흩어져 있어 타입으로 고정한다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class RequestContext:
    channel: str
    user: str
    ts: str
    thread_ts: str
    text: str
    files: tuple[Mapping[str, Any], ...] = ()
    unaddressed: bool = False
    late: bool = False
    requeued: bool = False
    queued_at: float | None = None
    first_reaction_at: float | None = None
    is_direct_message: bool = False
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        """중복 판정 키. 채널과 메시지 ts 의 쌍이다."""
        return (self.channel, self.ts)

    def marked_late(self) -> "RequestContext":
        return replace(self, late=True)

    def marked_requeued(self, queued_at: float) -> "RequestContext":
        return replace(self, requeued=True, queued_at=queued_at)

    def to_json(self) -> str:
        """큐의 payload 컬럼에 넣는 형식. from_json 으로 손실 없이 복원된다."""
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, payload: str) -> "RequestContext":
        data = json.loads(payload)
        data["files"] = tuple(data.get("files") or ())
        return cls(**data)
