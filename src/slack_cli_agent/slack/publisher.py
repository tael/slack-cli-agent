"""Posts replies and handles the failure paths: two rendering modes
per channel (rich markdown blocks vs. plain mrkdwn), chunk
verification with a safe fallback, falling back to plain text when
Slack rejects rich blocks, and a partial-delivery notice on failed
chunk sends.

ELAPSED_MODEL_LINE generalizes what used to be a company-specific
footer appending the model name for one hardcoded channel — now it's
driven by ChannelConfig.rich, so any rich channel gets it. The regex
itself comes from core.markers, not the guard layer, since the
publisher has no reason to depend on guard.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.channel_kind import is_direct_message_channel
from slack_cli_agent.core.errors import SlackError
from slack_cli_agent.core.markers import ELAPSED_MODEL_LINE
from slack_cli_agent.observability.audit import IncidentKind
from slack_cli_agent.render.blocks import BlockBuilder
from slack_cli_agent.render.markdown import MarkdownConverter
from slack_cli_agent.render.splitter import ContentSplitter
from slack_cli_agent.render.verifier import SplitVerifier

log = logging.getLogger(__name__)

# Color-codes status so success vs. failure is visible before reading the text.
COLOR_FAIL = "#d64541"


class MessagePublisher:
    def __init__(
        self,
        client: Any,
        settings: RuntimeSettings,
        markdown: MarkdownConverter,
        splitter: ContentSplitter,
        verifier: SplitVerifier,
        blocks: BlockBuilder,
        bot_display_name: str,
        audit: Callable[..., None] | None = None,
    ) -> None:
        self._client = client
        self._settings = settings
        self._markdown = markdown
        self._splitter = splitter
        self._verifier = verifier
        self._blocks = blocks
        self._bot_display_name = bot_display_name
        self._audit = audit or (lambda **_: None)

    def apply_elapsed_model_line(self, body: str, model: str, rich: bool) -> str:
        """Appends the model-name footer, only for rich channels with a model set.

        Strips any existing footer first — a model sometimes echoes
        prior context and writes this same line itself, and leaving
        both would produce two lines with possibly different values.
        """
        if not rich or not model:
            return body
        cleaned = ELAPSED_MODEL_LINE.sub("", body.rstrip()).rstrip()
        return cleaned.rstrip() + f"\n> 실행 모델 : {model}"

    def post(self, channel: str, thread_ts: str, text: str, rich: bool) -> str | None:
        """Posts using the channel's rendering mode.

        Rich channels get the markdown source verbatim in a markdown
        block; everything else gets it downgraded to mrkdwn. Channels
        reply in a thread; DMs post directly.
        """
        if rich:
            separated = self._verifier.separate_tables(text)
            chunks = self._splitter.split_for_blocks(separated)
            problems = self._verifier.verify_chunks(separated, chunks)
            if problems:
                self._audit(
                    kind=IncidentKind.SPLIT_BROKEN.value, channel=channel, thread_ts=thread_ts,
                    problems=problems, total=len(text),
                    sizes=[len(c) for c in chunks],
                )
                chunks = self._verifier.safe_fallback(separated)
            # separate_tables split tables and paragraphs into distinct
            # chunks, and split_for_blocks rejoined them, losing the
            # blank line between them. Re-separate now that
            # verification is done.
            chunks = [self._verifier.separate_tables(c) for c in chunks]
        else:
            chunks = self._splitter.chunk(self._markdown.to_mrkdwn(text))

        # Empty means "no thread yet", same as None. Leaving "" in would
        # never take the res["ts"] branch below, so the caller gets "" back
        # and later chunks post at top level instead of under the first.
        parent_ts = None if is_direct_message_channel(channel) else (thread_ts or None)
        sent = 0
        for idx, part in enumerate(chunks):
            kwargs: dict[str, Any] = {"channel": channel, "username": self._bot_display_name}
            note = ""
            if rich:
                if idx == len(chunks) - 1:
                    part, note = self._blocks.split_context(part)
                kwargs["text"] = self._blocks.preview(part or note)
                blocks: list[dict[str, Any]] = []
                if part.strip():
                    blocks.append({"type": "markdown", "text": part})
                if note:
                    blocks.append({
                        "type": "context",
                        "elements": [{"type": "mrkdwn", "text": note}],
                    })
                kwargs["blocks"] = blocks
            else:
                kwargs["text"] = part
            if parent_ts:
                kwargs["thread_ts"] = parent_ts

            try:
                res = self._client.chat_postMessage(**kwargs)
            except Exception as exc:
                if rich and self._verifier.blocks_rejected(exc):
                    self._audit(
                        kind=IncidentKind.BLOCKS_REJECTED.value, channel=channel,
                        thread_ts=thread_ts, error=str(exc), body=part[:2000],
                    )
                    plain = {k: v for k, v in kwargs.items() if k != "blocks"}
                    whole = f"{part}\n\n> {note}" if note else part
                    plain["text"] = self._markdown.to_mrkdwn(whole)[:3900]
                    try:
                        res = self._client.chat_postMessage(**plain)
                        sent += 1
                        if parent_ts is None:
                            parent_ts = res["ts"]
                        continue
                    except Exception as retry_exc:  # noqa: BLE001 - fold the retry failure into the original for failure reporting
                        exc = retry_exc

                self._audit(
                    kind=IncidentKind.POST_FAILED.value, channel=channel, thread_ts=thread_ts,
                    sent=sent, total=len(chunks), error=str(exc),
                )
                if sent:
                    try:
                        self._client.chat_postMessage(
                            channel=channel, username=self._bot_display_name,
                            thread_ts=parent_ts,
                            text=f"답변이 {sent}/{len(chunks)} 까지만 전달됐습니다.",
                            attachments=[{
                                "color": COLOR_FAIL,
                                "text": (
                                    f"답변이 {sent}/{len(chunks)} 까지만 전달됐습니다. "
                                    "나머지는 보내지 못했습니다."
                                ),
                            }],
                        )
                    except Exception as notify_exc:  # noqa: BLE001 - a failed failure-notice shouldn't block the original error report
                        log.warning("부분 발송 실패 안내 전송 실패 : %s", notify_exc)
                raise SlackError(str(exc)) from exc
            sent += 1
            if parent_ts is None:
                parent_ts = res["ts"]

        if rich and len(chunks) > 1:
            self._audit(
                kind=IncidentKind.SPLIT.value, channel=channel, thread_ts=thread_ts,
                total=len(text), sizes=[len(c) for c in chunks],
            )
        return parent_ts
