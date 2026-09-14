"""TranscriptBuilder — 지난 대화 복원.

원본 `thread_transcript`, `with_history`, `message_text`, `blocks_to_text`,
`rich_text_block_text`, `rich_text_element_text` 를 옮겼다. 대화의 원본은
클로드 세션 파일이 아니라 슬랙이다 — 세션이 만료되거나 끊기거나 새로
발급돼도 여기서 다시 세울 수 있다.

화자 이름 해석은 원본이 전역 캐시(`_asker_cache`)와 회사 고유 상수
(`OWNER_USER_ID`, `BOT_DISPLAY_NAME`)로 처리하던 부분이다. 생성자로 주입받는
`name_resolver` 로 재구성했다 — 이름 조회와 그 캐시는 이 클래스의 책임이
아니다. 화자 표시 문자열을 만드는 판정 자체는 `SpeakerNamer` 에 있다 —
`LateAddendumChecker` 와 같은 판정을 쓰게 하려는 목적이다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.observability.notices import NoticeCatalog
from slack_cli_agent.slack.identity import BotIdentity
from slack_cli_agent.slack.message_kind import MessageKind
from slack_cli_agent.slack.speaker import SpeakerNamer

from ..core.timezones import KST


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
            text_field = block.get("text")
            section_body = text_field.get("text") if isinstance(text_field, dict) else text_field
            if isinstance(section_body, str) and section_body:
                out.append(section_body)
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
        identity: BotIdentity,
        bot_display_name: str = "",
        owner_user_id: str = "",
        owner_display_name: str = "",
    ) -> None:
        self._client = client
        self._settings = settings
        self._notices = notices
        # 자기 말 판정 근거. 부품마다 따로 들면 조립이 일부에만 값을 줘도
        # 부품 시험이 통과해, 그 부품만 조용히 예전 판정으로 돌아간다.
        self._identity = identity
        # 화자 표시는 late_addendum 과 공유하는 SpeakerNamer 에 위임한다.
        # 봇 분기를 포함한 완전한 판정을 한 곳에 두려는 목적이다.
        self._speaker = SpeakerNamer(
            name_resolver=name_resolver,
            is_self=identity.is_self,
            bot_display_name=bot_display_name,
            owner_user_id=owner_user_id,
            owner_display_name=owner_display_name,
        )
        # 메시지를 무엇으로 볼지는 한 곳에서 판정한다. 여기 따로 적으면 기준이 갈린다.
        self._kind = MessageKind()

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
        except Exception:  # noqa: BLE001 — 기록 조회 실패로 전체를 막지 않고 빈 결과로 넘어간다
            return ""

        lines = []
        for m in msgs:
            if not self._kind.is_transcribable(m):
                continue
            if before_ts and float(m.get("ts", 0)) >= float(before_ts):
                continue
            if after_ts and float(m.get("ts", 0)) <= float(after_ts):
                continue
            text = message_text(m).strip()
            if not text or self._notices.is_notice(text):
                continue
            when = datetime.fromtimestamp(float(m.get("ts", 0)), KST).strftime("%H:%M:%S")
            who = self._speaker.speaker_of(m)
            lines.append(f"[{when} {who}]\n{text}")

        if not lines:
            return ""

        max_chars = self._settings.history_max_chars
        kept: list[str] = []
        total = 0
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
