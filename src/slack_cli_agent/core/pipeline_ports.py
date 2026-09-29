"""What RequestPipeline needs from its collaborators.

Each Protocol lists only the methods the pipeline actually calls. Typing the
constructor against the implementations instead made a stand-in impossible to
pass, so the tests passed one anyway and nothing checked it stayed in step with
the real class (sca-ve2).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from ..auth.principal import Principal
from ..config.channel import ChannelConfig
from ..slack.transcript import CurrentMessage


class AccessPolicyPort(Protocol):
    def principal_for(self, channel: str, user: str) -> Principal: ...

    def model_for(self, principal: Principal) -> str: ...

    def effort_for(self, principal: Principal, prompt: str) -> str: ...


class TranscriptPort(Protocol):
    def thread_transcript(
        self,
        channel: str,
        thread_ts: str,
        before_ts: str | float | None,
        scope: str = "thread",
        after_ts: str | float | None = None,
    ) -> str: ...

    def with_history(self, transcript: str, current: CurrentMessage) -> str: ...


class PublisherPort(Protocol):
    def apply_elapsed_model_line(self, body: str, model: str, rich: bool) -> str: ...

    def post(self, channel: str, thread_ts: str, text: str, rich: bool) -> str | None: ...


class AuditPort(Protocol):
    def record(self, kind: str, *, channel: str = "", thread_ts: str = "", **fields: Any) -> None: ...

    def record_request(
        self,
        *,
        channel: str,
        thread_ts: str,
        message_ts: str,
        session_id: str,
        resumed: bool,
        model: str,
        effort: str,
        elapsed: float,
        ok: bool,
        first_reaction_sec: float | None = None,
        queue_wait_sec: float | None = None,
        usage: Mapping[str, Any] | None = None,
        user: str = "",
        turns: int | None = None,
        **extra: Any,
    ) -> None: ...


class ChannelLookupPort(Protocol):
    def get(self, channel_id: str) -> ChannelConfig | None: ...


class ReactionPort(Protocol):
    def mark_processing(self, channel: str, ts: str) -> None: ...
