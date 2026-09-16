"""Slack agent panel (top bar) thread events.

Opening a new thread in the panel sends assistant_thread_started. Slack posts
nothing of its own there, so without this the panel opens empty and the person
has no idea what this bot can be asked (sca-kos.7).

Greeting and suggested prompts are per bot, so both come from the profile.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from ..config.profile import AgentPrompt

log = logging.getLogger(__name__)

#: Used when the profile sets no greeting of its own.
DEFAULT_GREETING = "무엇을 도와드릴까요? 이 스레드에 그대로 적어 주십시오."

# (channel, thread_ts, text)
ReplyCallback = Callable[[str, str, str], None]


class SuggestedPromptsClient(Protocol):
    def assistant_threads_setSuggestedPrompts(self, **kwargs: Any) -> Any: ...


class AssistantPanel:
    def __init__(
        self,
        client: SuggestedPromptsClient,
        reply: ReplyCallback,
        greeting: str,
        prompts: tuple[AgentPrompt, ...] = (),
    ) -> None:
        self._client = client
        self._reply = reply
        self._greeting = greeting
        self._prompts = prompts

    def thread_started(self, event: Mapping[str, Any]) -> None:
        thread = event.get("assistant_thread") or {}
        channel = str(thread.get("channel_id") or "")
        thread_ts = str(thread.get("thread_ts") or "")
        if not channel or not thread_ts:
            log.warning("에이전트 스레드 시작 이벤트에 자리가 없다 : %s", event.get("type"))
            return
        if self._greeting:
            self._reply(channel, thread_ts, self._greeting)
        self._set_prompts(channel, thread_ts)

    def _set_prompts(self, channel: str, thread_ts: str) -> None:
        # An empty list would clear the prompts the manifest declares, so a bot
        # with none configured leaves this call out entirely.
        if not self._prompts:
            return
        try:
            self._client.assistant_threads_setSuggestedPrompts(
                channel_id=channel,
                thread_ts=thread_ts,
                prompts=[{"title": p.title, "message": p.message} for p in self._prompts],
            )
        except Exception as exc:  # noqa: BLE001 - 안내는 이미 나갔다. 여기서 죽으면 그것까지 잃는다
            log.warning("제안 프롬프트를 걸지 못했다 : %s", exc)
