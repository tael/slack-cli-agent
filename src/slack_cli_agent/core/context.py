"""Immutable context for a single request."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from .jsonsafe import dump_json


@dataclass(frozen=True)
class RequestContext:
    channel: str
    user: str
    ts: str
    thread_ts: str
    text: str
    files: tuple[Mapping[str, Any], ...] = ()
    #: Slack 이 이 메시지의 링크를 펼친 미리보기(unfurl) 첨부. 봇이 못 읽는
    #: 채널 링크를 이 본문으로 복원하는 LinkedThreadReader 의 폴백이 쓴다(C39).
    #: files 와 달리 내려받지 않고 이벤트의 값을 그대로 큐까지 싣는다.
    unfurl_attachments: tuple[Mapping[str, Any], ...] = ()
    missed_files: int = 0
    unaddressed: bool = False
    late: bool = False
    requeued: bool = False
    queued_at: float | None = None
    first_reaction_at: float | None = None
    is_direct_message: bool = False
    #: The event carried a bot_id. The operator check reads this rather than a
    #: users_info lookup, which reads a failed call as "a person" (A9 review;
    #: mametchi bot.py:7855 is_bot=bool(event.get("bot_id"))).
    from_bot: bool = False
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return (self.channel, self.ts)

    @property
    def has_no_request(self) -> bool:
        """Nothing left to act on once the bot's own mention is stripped.

        Both the socket path and catch-up ask this before queueing; written
        twice, one side would be fixed and the other left behind (sca-zct1).
        Attachments do not count — the original checks the text before it
        saves them (bot.py:5075).
        """
        return not self.text.strip()

    def marked_late(self) -> RequestContext:
        return replace(self, late=True)

    def marked_requeued(self, queued_at: float) -> RequestContext:
        return replace(self, requeued=True, queued_at=queued_at)

    def with_first_reaction(self, at: float) -> RequestContext:
        """첫 상태 리액션(eyes)이 달린 시각을 찍는다. 접수(ts)부터 첫 반응까지의
        실측 시간(_first_reaction_sec)이 audit 로그에 남는다 (B38, 마메치 parity)."""
        return replace(self, first_reaction_at=at)

    def to_json(self) -> str:
        return dump_json(asdict(self))

    @classmethod
    def from_json(cls, payload: str) -> RequestContext:
        data = json.loads(payload)
        data["files"] = tuple(data.get("files") or ())
        data["unfurl_attachments"] = tuple(data.get("unfurl_attachments") or ())
        return cls(**data)
