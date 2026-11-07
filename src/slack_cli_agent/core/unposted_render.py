"""Catches a turn that rendered a Mermaid diagram but never posted it (D1).

The render tool only writes a local PNG. The model opens that file, sees the
diagram and answers as if it were shown, while the thread holds nothing.
2026-09-22 구치파치 rendered twice and posted neither time. ThreadCoordsSection
puts channel + thread_ts in the prompt so the posting tool can be called; this
is the after-the-fact half: one rerun in the same session that asks for the
post, and a plain notice in the body if that rerun still doesn't post.
Ported from mametchi bot.py rendered_without_posting / unposted_render_prompt.

Only an engine that reports its tool calls can be judged. Codex puts them in
``raw["tools"]`` as ``server.tool``; claude's json output carries none, so on
that engine both checks are always false and nothing changes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ..engine.base import EngineResponse

#: Matched on the last segment so codex ``mermaid.mermaid_render`` and claude
#: ``mcp__mermaid__mermaid_render`` read the same.
RENDER_ONLY_TOOLS: tuple[str, ...] = ("mermaid_render",)
RENDER_POST_TOOLS: tuple[str, ...] = ("mermaid_render_and_post",)

NOTICE_UNPOSTED_RENDER = "도식 파일은 만들었지만 이 스레드에 올리지 못했습니다. 다시 요청해 주세요."


def tool_names(response: EngineResponse) -> list[str]:
    """Last name segment of every tool the turn called, or [] when unreported."""
    raw = response.raw if isinstance(response.raw, Mapping) else {}
    tools = raw.get("tools")
    if not isinstance(tools, Sequence) or isinstance(tools, (str, bytes)):
        return []
    return [str(t).replace("__", ".").rsplit(".", 1)[-1] for t in tools]


def rendered_without_posting(response: EngineResponse) -> bool:
    names = tool_names(response)
    return any(n in RENDER_ONLY_TOOLS for n in names) and not any(
        n in RENDER_POST_TOOLS for n in names
    )


def rendered_and_posted(response: EngineResponse) -> bool:
    return any(n in RENDER_POST_TOOLS for n in tool_names(response))


def unposted_render_prompt(channel: str, thread_ts: str) -> str:
    return (
        "방금 턴에서 Mermaid 원문을 PNG 로 렌더했지만 슬랙에는 올리지 않았다.\n"
        "그 파일은 사람 화면에 나타나지 않는다. 지금 사용자에게는 아무것도 "
        "보이지 않는 상태다.\n"
        "이 지시는 봇 코드가 보낸 것이다. 사용자가 쓴 말이 아니다.\n"
        f"같은 원문으로 mermaid_render_and_post 를 channel={channel}, "
        f"thread_ts={thread_ts} 로 불러 이 스레드에 올려라.\n"
        "comment 에는 Mermaid 원문을 코드블록으로 담는다.\n"
        "올린 뒤에는 한 문장으로만 답한다. 앞 답을 다시 쓰지 않는다.\n"
        "[[NOWATCH]] 로 끝낸다."
    )
