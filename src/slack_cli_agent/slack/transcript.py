"""TranscriptBuilder — 지난 대화 복원.

원본 `thread_transcript`, `with_history`, `message_text`, `blocks_to_text`,
`rich_text_block_text`, `rich_text_element_text` 를 옮겼다. 대화의 원본은
클로드 세션 파일이 아니라 슬랙이다 — 세션이 만료되거나 끊기거나 새로
발급돼도 여기서 다시 세울 수 있다.

화자 이름 해석은 원본이 전역 캐시(`_asker_cache`)와 회사 고유 상수
(`OWNER_USER_ID`, `BOT_DISPLAY_NAME`)로 처리하던 부분이다. 생성자로 주입받는
`name_resolver` 로 재구성했다 — 이름 조회와 그 캐시는 이 클래스의 책임이
아니다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.observability.notices import NoticeCatalog

KST = timezone(timedelta(hours=9))


def rich_text_element_text(el: Mapping[str, Any]) -> str:
    """rich_text 블록 안의 조각 하나를 글자로 되살린다."""
    kind = el.get("type")
    if kind == "text":
        return el.get("text") or ""
    if kind == "link":
        label = el.get("text") or ""
        url = el.get("url") or ""
        return f"[{label}]({url})" if label and label != url else url
    if kind == "user":
        return f"<@{el.get('user_id')}>"
    if kind == "usergroup":
        return f"<!subteam^{el.get('usergroup_id')}>"
    if kind == "channel":
        return f"<#{el.get('channel_id')}>"
    if kind == "emoji":
        return el.get("unicode_text") or f":{el.get('name')}:"
    if kind == "broadcast":
        return f"<!{el.get('range')}>"
    return ""


def rich_text_block_text(block: Mapping[str, Any]) -> str:
    """rich_text 블록 하나를 여러 줄로 되살린다."""
    lines = []
    for el in block.get("elements") or []:
        kind = el.get("type")
        if kind == "rich_text_section":
            body = "".join(rich_text_element_text(x) for x in el.get("elements") or [])
            if body.strip():
                lines.append(body.rstrip())
        elif kind == "rich_text_list":
            indent = "  " * (el.get("indent") or 0)
            ordered = el.get("style") == "ordered"
            for i, item in enumerate(el.get("elements") or [], 1):
                body = "".join(
                    rich_text_element_text(x) for x in item.get("elements") or []
                )
                mark = f"{i}." if ordered else "-"
                lines.append(f"{indent}{mark} {body}".rstrip())
        elif kind == "rich_text_quote":
            body = "".join(rich_text_element_text(x) for x in el.get("elements") or [])
            lines.append("> " + body)
        elif kind == "rich_text_preformatted":
            body = "".join(rich_text_element_text(x) for x in el.get("elements") or [])
            lines.append("```\n" + body + "\n```")
    return "\n".join(lines)


def blocks_to_text(blocks: Any) -> str:
    """블록에서 본문을 되살린다.

    리치로 보낸 말은 `text` 필드에 알림 미리보기 한 줄만 담긴다. 본문은
    `blocks` 에 있다. `text` 만 읽으면 지난 답을 한 줄로 보고 스스로 잘려
    나갔다고 오판한다.
    """
    out = []
    for block in blocks or []:
        kind = block.get("type")
        if kind == "header":
            head = (block.get("text") or {}).get("text") or ""
            if head:
                out.append("#" * (block.get("level") or 2) + " " + head)
        elif kind == "rich_text":
            body = rich_text_block_text(block)
            if body:
                out.append(body)
        elif kind in ("section", "markdown"):
            body = block.get("text")
            body = body.get("text") if isinstance(body, dict) else body
            if body:
                out.append(body)
        elif kind == "context":
            parts = [
                (e.get("text") or "") for e in block.get("elements") or []
                if isinstance(e, dict) and e.get("type") in ("mrkdwn", "plain_text")
            ]
            body = " ".join(p for p in parts if p)
            if body:
                out.append(body)
        elif kind == "divider":
            out.append("---")
        elif kind == "table":
            for row in block.get("rows") or []:
                cells = [
                    rich_text_block_text(c) if isinstance(c, dict) else ""
                    for c in row or []
                ]
                out.append(" | ".join(cells))
    return "\n\n".join(out).strip()


def message_text(msg: Mapping[str, Any]) -> str:
    """슬랙 메시지 하나의 본문을 읽는다."""
    body = blocks_to_text(msg.get("blocks"))
    return body or (msg.get("text") or "")


class TranscriptBuilder:
    """슬랙 기록을 화자 표시가 붙은 대화록으로 되살린다."""

    def __init__(
        self,
        client: Any,
        settings: RuntimeSettings,
        notices: NoticeCatalog,
        name_resolver: Callable[[str], str],
        bot_user_id: str = "",
        bot_id: str = "",
        bot_display_name: str = "",
        owner_user_id: str = "",
        owner_display_name: str = "",
    ) -> None:
        self._client = client
        self._settings = settings
        self._notices = notices
        self._name_resolver = name_resolver
        self._bot_user_id = bot_user_id
        self._bot_id = bot_id
        self._bot_display_name = bot_display_name
        self._owner_user_id = owner_user_id
        self._owner_display_name = owner_display_name

    def _is_self(self, msg: Mapping[str, Any]) -> bool:
        if self._bot_id and msg.get("bot_id"):
            return msg.get("bot_id") == self._bot_id
        if self._bot_user_id and msg.get("user"):
            return msg.get("user") == self._bot_user_id
        return bool(msg.get("bot_id"))

    def _speaker_of(self, msg: Mapping[str, Any]) -> str:
        if self._is_self(msg):
            return self._bot_display_name or "봇"
        if msg.get("bot_id"):
            profile = msg.get("bot_profile") or {}
            name = profile.get("name") or msg.get("username") or ""
            return f"{name} (다른 봇)" if name else "이름 모르는 봇"
        user = msg.get("user") or ""
        if user == self._owner_user_id and self._owner_display_name:
            return self._owner_display_name
        name = self._name_resolver(user) or "이름 모르는 사람"
        return f"{name} <@{user}>" if user else name

    def thread_transcript(
        self,
        channel: str,
        thread_ts: str,
        before_ts: str | float | None,
        scope: str = "thread",
        after_ts: str | float | None = None,
    ) -> str:
        """슬랙에서 지난 대화를 읽어 화자 표시가 붙은 기록으로 만든다.

        after_ts 를 주면 그 시각 이후로 오간 말만 담는다. 이어가는 세션에서
        스레드 전체를 매번 다시 읽지 않고, 그 사이 새로 오간 만큼만 더 읽어
        붙일 때 쓴다.
        """
        limit = self._settings.history_max_msgs
        try:
            if scope == "channel":
                msgs = self._client.conversations_history(
                    channel=channel, limit=limit
                ).get("messages", [])
                msgs = list(reversed(msgs))
            else:
                msgs = self._client.conversations_replies(
                    channel=channel, ts=thread_ts, limit=limit
                ).get("messages", [])
        except Exception:
            return ""

        lines = []
        for m in msgs:
            if m.get("subtype") and not m.get("bot_id"):
                continue
            if before_ts and float(m.get("ts", 0)) >= float(before_ts):
                continue
            if after_ts and float(m.get("ts", 0)) <= float(after_ts):
                continue
            text = message_text(m).strip()
            if not text or self._notices.is_notice(text):
                continue
            when = datetime.fromtimestamp(float(m.get("ts", 0)), KST).strftime("%H:%M:%S")
            who = self._speaker_of(m)
            lines.append(f"[{when} {who}]\n{text}")

        if not lines:
            return ""

        max_chars = self._settings.history_max_chars
        kept, total = [], 0
        for line in reversed(lines):
            total += len(line)
            if total > max_chars and kept:
                break
            kept.append(line)
        return "\n\n".join(reversed(kept))

    def with_history(self, transcript: str, tagged: str) -> str:
        """지난 대화를 지금 말 앞에 붙인다. 지난 대화에 다시 답하지 않게 못박는다."""
        if not transcript:
            return tagged
        return (
            "아래는 이 대화에서 지금까지 오간 말이다. 맥락을 잡는 데만 쓴다.\n"
            "이미 지나간 말이므로 여기에 다시 답하지 않는다.\n"
            "대괄호 안이 시각과 그 말을 한 사람이다.\n\n"
            "===== 지난 대화 =====\n\n"
            f"{transcript}\n\n"
            "===== 여기까지가 지난 대화다 =====\n\n"
            f"{tagged}"
        )
