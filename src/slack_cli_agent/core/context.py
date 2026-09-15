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
    unaddressed: bool = False
    late: bool = False
    requeued: bool = False
    queued_at: float | None = None
    first_reaction_at: float | None = None
    is_direct_message: bool = False
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return (self.channel, self.ts)

    def marked_late(self) -> RequestContext:
        return replace(self, late=True)

    def marked_requeued(self, queued_at: float) -> RequestContext:
        return replace(self, requeued=True, queued_at=queued_at)

    def to_json(self) -> str:
        return dump_json(asdict(self))

    @classmethod
    def from_json(cls, payload: str) -> RequestContext:
        data = json.loads(payload)
        data["files"] = tuple(data.get("files") or ())
        return cls(**data)
