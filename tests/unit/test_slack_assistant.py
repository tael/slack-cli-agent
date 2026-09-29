"""에이전트 패널이 열렸을 때의 첫 안내(sca-kos.7).

슬랙 상단바 에이전트 패널에서 새 스레드를 열면 assistant_thread_started 가 온다.
이것을 안 받으면 패널이 빈 채로 열리고 사람은 무엇을 물어야 할지 모른다.
"""

from __future__ import annotations

from typing import Any

from slack_cli_agent.config.profile import AgentPrompt
from slack_cli_agent.slack.assistant import AssistantPanel


class Fake클라이언트:
    def __init__(self, *, raise_on_prompts: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._raise = raise_on_prompts

    def assistant_threads_setSuggestedPrompts(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("prompts", kwargs))
        if self._raise:
            raise RuntimeError("슬랙이 거부했다")
        return {"ok": True}


def 패널(
    client: Fake클라이언트,
    replies: list[tuple[str, str, str]],
    *,
    greeting: str = "무엇을 도와드릴까요?",
    prompts: tuple[AgentPrompt, ...] = (),
) -> AssistantPanel:
    return AssistantPanel(
        client=client,
        reply=lambda channel, thread_ts, text: replies.append((channel, thread_ts, text)),
        greeting=greeting,
        prompts=prompts,
    )


def 시작이벤트(channel: str = "D1", thread_ts: str = "1.1") -> dict[str, Any]:
    return {
        "type": "assistant_thread_started",
        "assistant_thread": {
            "user_id": "U1",
            "context": {},
            "channel_id": channel,
            "thread_ts": thread_ts,
        },
    }


class Test첫안내:
    def test_스레드에_안내를_올린다(self) -> None:
        replies: list[tuple[str, str, str]] = []
        패널(Fake클라이언트(), replies).thread_started(시작이벤트())
        assert replies == [("D1", "1.1", "무엇을 도와드릴까요?")]

    def test_안내_문구가_비면_아무것도_안_올린다(self) -> None:
        replies: list[tuple[str, str, str]] = []
        패널(Fake클라이언트(), replies, greeting="").thread_started(시작이벤트())
        assert replies == []

    def test_제안_프롬프트를_그_스레드에_건다(self) -> None:
        client = Fake클라이언트()
        패널(
            client, [],
            prompts=(AgentPrompt(title="오늘 할 일", message="오늘 할 일을 알려줘"),),
        ).thread_started(시작이벤트())
        assert client.calls[0][0] == "prompts"
        인자 = client.calls[0][1]
        assert 인자["channel_id"] == "D1"
        assert 인자["thread_ts"] == "1.1"
        assert 인자["prompts"] == [{"title": "오늘 할 일", "message": "오늘 할 일을 알려줘"}]

    def test_제안이_없으면_슬랙을_안_부른다(self) -> None:
        """빈 목록을 보내면 매니페스트에 적힌 기본 제안까지 지워진다."""
        client = Fake클라이언트()
        패널(client, []).thread_started(시작이벤트())
        assert client.calls == []

    def test_제안_설정이_실패해도_안내는_남는다(self) -> None:
        """둘은 별개다. 하나가 실패해서 이벤트 처리가 죽으면 패널이 빈 채로 남는다."""
        replies: list[tuple[str, str, str]] = []
        패널(
            Fake클라이언트(raise_on_prompts=True), replies,
            prompts=(AgentPrompt(title="ㄱ", message="ㄴ"),),
        ).thread_started(시작이벤트())
        assert replies == [("D1", "1.1", "무엇을 도와드릴까요?")]

    def test_자리를_모르는_이벤트는_아무_일도_하지_않는다(self) -> None:
        replies: list[tuple[str, str, str]] = []
        client = Fake클라이언트()
        패널(client, replies).thread_started({"type": "assistant_thread_started"})
        assert replies == []
        assert client.calls == []
