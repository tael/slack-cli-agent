"""Rebuilds past conversation from Slack history.

Slack, not the Claude session file, is the source of truth for the
conversation — sessions expire, disconnect, or get reissued, but this
can always reconstruct from Slack.

Speaker name resolution used to go through a global cache and
company-specific constants; now name_resolver is constructor-injected,
and building the speaker display string itself lives in SpeakerNamer,
shared with LateAddendumChecker so both use the same judgment.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.observability.notices import NoticeCatalog
from slack_cli_agent.slack.identity import BotIdentity
from slack_cli_agent.slack.mentions import MentionRenderer
from slack_cli_agent.slack.message_kind import MessageKind
from slack_cli_agent.slack.speaker import SpeakerNamer

from ..core.timezones import KST

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TranscriptRead:
    """A read that failed and a thread with nothing to transcribe both produce
    an empty body. Callers that tell the model what happened need them apart
    (sca-678)."""

    body: str
    read_ok: bool


def rich_text_element_text(el: Mapping[str, Any]) -> str:
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
    """Reconstructs the body text from a message's blocks.

    A rich-sent message only has a one-line notification preview in
    `text` — the real body is in `blocks`. Reading only `text` makes a
    past reply look truncated to one line.
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
    body = blocks_to_text(msg.get("blocks"))
    return body or (msg.get("text") or "")


class TranscriptBuilder:
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
        group_resolver: Callable[[str], str] | None = None,
    ) -> None:
        self._client = client
        self._settings = settings
        self._notices = notices
        # Self-message detection must use the same identity source
        # everywhere — holding it separately per component would let
        # wiring miss one and silently fall back to old behavior
        # while unit tests still pass.
        self._identity = identity
        # Speaker display delegates to the SpeakerNamer shared with
        # late_addendum, so both use the same judgment.
        self._speaker = SpeakerNamer(
            name_resolver=name_resolver,
            is_self=identity.is_self,
            bot_display_name=bot_display_name,
            owner_user_id=owner_user_id,
            owner_display_name=owner_display_name,
        )
        # Message classification lives in one place; duplicating it
        # here would let the criteria drift.
        self._kind = MessageKind()
        # The body kept raw mention markup, so the model could not tell who
        # called whom (sca-hkmb).
        self._mentions = MentionRenderer(name_resolver, group_resolver)

    def thread_transcript(
        self,
        channel: str,
        thread_ts: str,
        before_ts: str | float | None,
        scope: str = "thread",
        after_ts: str | float | None = None,
    ) -> str:
        """Reads Slack history and formats it as a transcript with speaker labels.

        Pass after_ts to only include messages since then — used when
        continuing a session, to append what's new since the last
        read instead of re-reading the whole thread every time.
        """
        return self.read_thread(channel, thread_ts, before_ts, scope=scope, after_ts=after_ts).body

    def read_thread(
        self,
        channel: str,
        thread_ts: str,
        before_ts: str | float | None,
        scope: str = "thread",
        after_ts: str | float | None = None,
    ) -> TranscriptRead:
        """thread_transcript plus whether the read itself succeeded."""
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
        except Exception as exc:  # noqa: BLE001 - a failed history read falls back to empty rather than raising
            log.warning("대화록 조회 실패 : channel=%s scope=%s error=%s", channel, scope, exc)
            return TranscriptRead(body="", read_ok=False)

        lines = []
        for m in msgs:
            if not self._kind.is_transcribable(m):
                continue
            if before_ts and float(m.get("ts", 0)) >= float(before_ts):
                continue
            if after_ts and float(m.get("ts", 0)) <= float(after_ts):
                continue
            text = message_text(m).strip()
            # The notice check runs on the raw text: a notice may itself carry
            # mention markup, and rendering first would stop matching it.
            if not text or self._notices.is_notice(text):
                continue
            text = self._mentions.render(text)
            when = datetime.fromtimestamp(float(m.get("ts", 0)), KST).strftime("%H:%M:%S")
            who = self._speaker.speaker_of(m)
            lines.append(f"[{when} {who}]\n{text}")

        if not lines:
            return TranscriptRead(body="", read_ok=True)

        max_chars = self._settings.history_max_chars
        kept: list[str] = []
        total = 0
        for line in reversed(lines):
            total += len(line)
            if total > max_chars and kept:
                break
            kept.append(line)
        return TranscriptRead(body="\n\n".join(reversed(kept)), read_ok=True)

    def with_history(self, transcript: str, tagged: str) -> str:
        """Prepends past conversation ahead of the current message,
        with an explicit instruction not to re-answer it."""
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
