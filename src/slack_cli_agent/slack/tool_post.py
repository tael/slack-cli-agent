"""Folds the final answer into a file message the engine already posted.

An engine that answers with a file posts it itself, mid-turn, through an MCP
tool (csv_post_from_sql, mermaid_render_and_post, image_post_*). That upload
lands in the thread as a message of its own, with the tool's comment as its
text. The pipeline then posted the final answer as a second message, so one
question got two replies: the file with a half-written comment, and the real
answer below it. 2026-11-06 데이전트 did this on every CSV request in the test
channel (17:10, 17:22), 구치파치 the same, and mametchi had it until bot.py
0dcd540 dropped the second post.

Dropping the second post keeps the tool's comment as the answer, and that
comment is often process talk ("이 스레드의 원 요청과 CSV 수신 위치를
slack_thread_replies로 확인했습니다"). So the final answer is written over the
file message instead: chat.update keeps the attached file and replaces only
the text, leaving one reply that holds both.

Engine-agnostic on purpose. Codex reports no tool names to the pipeline, so
the trace is read from the thread itself: a message this bot posted during
the turn that carries files.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from ..observability.audit import IncidentKind

log = logging.getLogger(__name__)

#: Slack ts and the turn clock come from different machines; a small slack
#: keeps a tool post made in the first instant of the turn from being missed.
CLOCK_SKEW_SEC = 2.0

#: How many replies to read back. The tool post is recent, so the tail of a
#: normal thread is enough.
REPLIES_LIMIT = 200


@runtime_checkable
class ToolPostMerger(Protocol):
    def merge(
        self, channel: str, thread_ts: str, since: float, body: str, rich: bool
    ) -> str | None:
        """Returns the ts the answer was written into, or None to post as usual."""
        ...


class SlackToolPostMerger:
    def __init__(
        self,
        client: Any,
        is_self: Callable[[Mapping[str, Any]], bool],
        update: Callable[[str, str, str, bool], Any],
        audit: Callable[..., None] | None = None,
    ) -> None:
        self._client = client
        self._is_self = is_self
        self._update = update
        self._audit = audit or (lambda **_: None)

    def find(self, channel: str, thread_ts: str, since: float) -> str | None:
        """The latest file message this bot posted in the thread since ``since``."""
        try:
            res = self._client.conversations_replies(
                channel=channel, ts=thread_ts, limit=REPLIES_LIMIT
            )
        except Exception as exc:  # noqa: BLE001 - unknown means post as usual
            log.warning("도구 게시 확인 실패 channel=%s ts=%s : %s", channel, thread_ts, exc)
            return None
        found: str | None = None
        for msg in (res or {}).get("messages") or []:
            ts = str(msg.get("ts") or "")
            if not ts or ts == thread_ts:
                continue
            try:
                posted_at = float(ts)
            except ValueError:
                continue
            if posted_at < since - CLOCK_SKEW_SEC:
                continue
            if not msg.get("files") or not self._is_self(msg):
                continue
            if found is None or posted_at > float(found):
                found = ts
        return found

    def merge(
        self, channel: str, thread_ts: str, since: float, body: str, rich: bool
    ) -> str | None:
        ts = self.find(channel, thread_ts, since)
        if ts is None:
            return None
        try:
            self._update(channel, ts, body, rich)
        except Exception as exc:  # noqa: BLE001 - a failed rewrite falls back to a normal post
            log.warning("파일 게시 메시지에 답을 합치지 못했다 ts=%s : %s", ts, exc)
            self._audit(
                kind=IncidentKind.TOOL_POST_MERGE_FAILED.value, channel=channel,
                thread_ts=thread_ts, target_ts=ts, error=str(exc)[:300],
            )
            return None
        self._audit(
            kind=IncidentKind.TOOL_POST_MERGED.value, channel=channel, thread_ts=thread_ts,
            target_ts=ts, snippet=body[:200],
        )
        return ts
