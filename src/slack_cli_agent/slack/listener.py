"""EventListener — 슬랙 이벤트를 RequestContext 로 변환.

원본 `on_mention`, `on_message`, `on_reaction` 세 핸들러가 하던 판정을
옮겼다.

- `app_mention` — 그대로 받는다
- `message` — DM 은 바로 받는다. 채널은 스레드 답글일 때만, 그것도 봇이
  이미 그 스레드에 낀 경우에만 받는다. 멘션이 있으면 `app_mention` 이 이미
  받으므로 중복 처리하지 않는다
- `reaction_added` — 등록된 이모지가 봇 자신의 답변에 달렸을 때만 받는다

원본 `mention_only(channel)` 은 기본값 True(이름을 불러야만 답한다)였다.
`ChannelConfig.answer_unaddressed` 는 그 반대 의미로 이미 있는 필드이고
기본값 False 라 그대로 대응된다 — 채널 설정이 없거나 `answer_unaddressed`
가 꺼져 있으면 이름을 불러야만 답한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from slack_cli_agent.config.channel import ChannelRegistry
from slack_cli_agent.core.context import RequestContext
from slack_cli_agent.slack.gate import ResponseGate
from slack_cli_agent.slack.identity import BotIdentity
from slack_cli_agent.slack.message_kind import MessageKind


class EventListener:
    """슬랙 이벤트 하나를 받아 처리할지 판단하고, 처리한다면 RequestContext 로 만든다."""

    def __init__(
        self,
        client: Any,
        channel_registry: ChannelRegistry,
        gate: ResponseGate,
        identity: BotIdentity,
    ) -> None:
        self._client = client
        self._channels = channel_registry
        self._gate = gate
        # 자기 말 판정과 멘션 대조가 같은 신원을 봐야 한다. 따로 들면 조립이
        # 한쪽에만 값을 줘도 부품 시험이 통과한다.
        self._identity = identity
        # 메시지를 무엇으로 볼지는 한 곳에서 판정한다. 여기 따로 적으면 기준이 갈린다.
        self._kind = MessageKind()

    def _context_from_event(
        self, event: Mapping[str, Any], *, unaddressed: bool, is_dm: bool
    ) -> RequestContext:
        ts = event.get("ts") or ""
        return RequestContext(
            channel=event.get("channel") or "",
            user=event.get("user") or "",
            ts=ts,
            thread_ts=event.get("thread_ts") or ts,
            text=event.get("text") or "",
            files=tuple(event.get("files") or ()),
            unaddressed=unaddressed,
            is_direct_message=is_dm,
        )

    def from_app_mention(self, event: Mapping[str, Any]) -> RequestContext:
        return self._context_from_event(event, unaddressed=False, is_dm=False)

    def _thread_state(self, channel: str, thread_ts: str) -> tuple[bool, bool]:
        """스레드에서 봇의 위치를 본다. (이미 끼었는가, 마지막 말이 되물음인가)."""
        try:
            replies = self._client.conversations_replies(
                channel=channel, ts=thread_ts, limit=30
            )
        except Exception:  # noqa: BLE001 — 스레드 위치 조회 실패를 아직 안 낀 것으로 본다 — 조회 제한 초과도 이 경로로 들어온다
            return False, False
        msgs = replies.get("messages", [])
        joined = any(self._identity.is_self(m) for m in msgs)
        last_self = next((m for m in reversed(msgs) if self._identity.is_self(m)), None)
        asked = self._gate.asked_back(last_self.get("text") if last_self else "")
        return joined, asked

    def from_message(self, event: Mapping[str, Any]) -> RequestContext | None:
        if not self._kind.is_human(event):
            return None

        if event.get("channel_type") == "im":
            return self._context_from_event(event, unaddressed=False, is_dm=True)

        channel = event.get("channel") or ""
        thread_ts = event.get("thread_ts")
        if not thread_ts:
            # 스레드 답글이 아니면 부르지 않은 것이다
            return None

        text = event.get("text") or ""
        if self._identity.user_id and f"<@{self._identity.user_id}>" in text:
            # 멘션이 있으면 app_mention 이 이미 받는다. 두 번 처리하지 않는다
            return None

        config = self._channels.get(channel)
        if config is None or not config.answer_unaddressed:
            return None

        joined, bot_asked = self._thread_state(channel, thread_ts)
        if not joined:
            return None
        if not self._gate.worth_answering(text, bot_asked):
            return None

        return self._context_from_event(event, unaddressed=True, is_dm=False)

    def from_reaction(
        self, event: Mapping[str, Any], allowed: frozenset[str]
    ) -> tuple[str, str, str, str] | None:
        """리액션 이벤트에서 (이모지, 채널, 메시지시각, 누른사람) 을 뽑는다.

        대상 이모지가 아니거나, 메시지가 아니거나, 봇 자신의 답변이 아니거나,
        봇 자신이 누른 것이면 받지 않는다.
        """
        reaction = event.get("reaction")
        if reaction not in allowed:
            return None
        item = event.get("item") or {}
        if item.get("type") != "message":
            return None
        if self._identity.user_id and event.get("item_user") != self._identity.user_id:
            return None
        if self._identity.user_id and event.get("user") == self._identity.user_id:
            return None
        return reaction, item.get("channel") or "", item.get("ts") or "", event.get("user") or ""
