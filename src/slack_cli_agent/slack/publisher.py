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
from dataclasses import dataclass, field
from typing import Any

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.channel_kind import is_direct_message_channel
from slack_cli_agent.core.errors import SlackError
from slack_cli_agent.core.markers import ELAPSED_MODEL_LINE
from slack_cli_agent.observability.audit import IncidentKind
from slack_cli_agent.render.blocks import BlockBuilder
from slack_cli_agent.render.markdown import MarkdownConverter
from slack_cli_agent.render.splitter import ContentSplitter, block_cost, split_by_block_budget
from slack_cli_agent.render.verifier import SplitVerifier

log = logging.getLogger(__name__)

# Color-codes status so success vs. failure is visible before reading the text.
COLOR_FAIL = "#d64541"


@dataclass(frozen=True)
class RichPayload:
    """One chunk rendered for a rich channel.

    `body` and `note` are kept alongside the Slack arguments because the
    plain-text fallback in post() re-sends the same chunk without blocks
    and needs the two parts split_context() separated.
    """

    body: str
    note: str
    text: str
    blocks: list[dict[str, Any]] = field(default_factory=list)


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

    def _rich_payload(self, part: str, *, split_note: bool) -> RichPayload:
        """Renders one chunk into Slack arguments for a rich channel.

        post() and update() share this so a correction is rendered exactly
        the way the original answer was — two copies of this would let the
        two drift apart one edit at a time.
        """
        note = ""
        if split_note:
            part, note = self._blocks.split_context(part)
        blocks: list[dict[str, Any]] = []
        if part.strip():
            blocks.append({"type": "markdown", "text": part})
        if note:
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": note}],
            })
        return RichPayload(
            body=part, note=note, text=self._blocks.preview(part or note), blocks=blocks
        )

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
                    problems=[p.reason for p in problems], total=len(text),
                    sizes=[len(c) for c in chunks],
                    evidence=[
                        {"reason": p.reason, "line_no": p.line_no, "excerpt": p.excerpt}
                        for p in problems
                    ],
                )
                chunks = self._verifier.safe_fallback(separated)
            # separate_tables split tables and paragraphs into distinct
            # chunks, and split_for_blocks rejoined them, losing the
            # blank line between them. Re-separate now that
            # verification is done.
            chunks = [self._verifier.separate_tables(c) for c in chunks]
        else:
            chunks = self._splitter.chunk(self._markdown.to_mrkdwn(text))

        # An empty body makes split_for_blocks() return nothing, and the send
        # loop below then does nothing at all — indistinguishable from a
        # successful post. The caller decides what to do; this only records it.
        if not any(chunk.strip() for chunk in chunks):
            log.warning("빈 본문이라 게시하지 않았다 : 채널 %s, 스레드 %s", channel, thread_ts or "없음")
            return None

        # Empty means "no thread yet", same as None. Leaving "" in would
        # never take the res["ts"] branch below, so the caller gets "" back
        # and later chunks post at top level instead of under the first.
        parent_ts = None if is_direct_message_channel(channel) else (thread_ts or None)
        sent = 0
        # A rejected chunk can be replaced in place by smaller pieces, so this
        # walks a mutable list rather than the original chunks (sca-2k7).
        pending = list(chunks)
        idx = 0
        while idx < len(pending):
            part = pending[idx]
            kwargs: dict[str, Any] = {"channel": channel, "username": self._bot_display_name}
            note = ""
            if rich:
                payload = self._rich_payload(part, split_note=idx == len(pending) - 1)
                part, note = payload.body, payload.note
                kwargs["text"] = payload.text
                kwargs["blocks"] = payload.blocks
            else:
                kwargs["text"] = part
            if parent_ts:
                kwargs["thread_ts"] = parent_ts

            try:
                res = self._client.chat_postMessage(**kwargs)
            except Exception as exc:
                if rich and self._verifier.block_limit_exceeded(exc):
                    pieces = self._resplit(part)
                    if len(pieces) > 1:
                        self._audit(
                            kind=IncidentKind.BLOCKS_RESPLIT.value, channel=channel,
                            thread_ts=thread_ts, pieces=len(pieces),
                            cost=block_cost(part), sizes=[len(piece) for piece in pieces],
                        )
                        pending[idx : idx + 1] = pieces
                        continue
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
                        idx += 1
                        if parent_ts is None:
                            parent_ts = res["ts"]
                        continue
                    except Exception as retry_exc:  # noqa: BLE001 - fold the retry failure into the original for failure reporting
                        exc = retry_exc

                self._audit(
                    kind=IncidentKind.POST_FAILED.value, channel=channel, thread_ts=thread_ts,
                    sent=sent, total=len(pending), error=str(exc),
                )
                if sent:
                    try:
                        self._client.chat_postMessage(
                            channel=channel, username=self._bot_display_name,
                            thread_ts=parent_ts,
                            text=f"답변이 {sent}/{len(pending)} 까지만 전달됐습니다.",
                            attachments=[{
                                "color": COLOR_FAIL,
                                "text": (
                                    f"답변이 {sent}/{len(pending)} 까지만 전달됐습니다. "
                                    "나머지는 보내지 못했습니다."
                                ),
                            }],
                        )
                    except Exception as notify_exc:  # noqa: BLE001 - a failed failure-notice shouldn't block the original error report
                        log.warning("부분 발송 실패 안내 전송 실패 : %s", notify_exc)
                raise SlackError(str(exc)) from exc
            sent += 1
            idx += 1
            if parent_ts is None:
                parent_ts = res["ts"]

        if rich and len(pending) > 1:
            self._audit(
                kind=IncidentKind.SPLIT.value, channel=channel, thread_ts=thread_ts,
                total=len(text), sizes=[len(c) for c in pending],
            )
        return parent_ts

    @staticmethod
    def _resplit(part: str) -> list[str]:
        """Halves one chunk's block budget. Slack counted more blocks than the
        local estimate did, so the next try aims well under what it just was."""
        return split_by_block_budget(part, max(1, block_cost(part) // 2))

    def update(self, channel: str, ts: str, text: str, rich: bool) -> list[str]:
        """Rewrites one already-posted message, using the channel's mode.

        Rewrites only — it never posts. chat.update touches a single ts, so a
        correction that no longer fits in one message is refused rather than
        sent as its first chunk with the rest dropped. Returns the block types
        Slack was given, so the caller can report what the message became
        ([] for a plain-text channel).
        """
        if rich:
            body = self._verifier.separate_tables(text)
            chunks = self._splitter.split_for_blocks(body)
        else:
            chunks = self._splitter.chunk(self._markdown.to_mrkdwn(text))
        if not any(chunk.strip() for chunk in chunks):
            raise SlackError("교정본이 비어 있어 갱신하지 않았다")
        if len(chunks) > 1:
            raise SlackError(
                f"교정본이 한 메시지에 들어가지 않는다 : {len(chunks)} 조각, {len(text)}자. "
                "메시지 하나만 고칠 수 있으므로 갱신하지 않았다"
            )

        kwargs: dict[str, Any] = {"channel": channel, "ts": ts}
        if rich:
            payload = self._rich_payload(
                self._verifier.separate_tables(chunks[0]), split_note=True
            )
            kwargs["text"] = payload.text
            kwargs["blocks"] = payload.blocks
        else:
            kwargs["text"] = chunks[0]

        try:
            self._client.chat_update(**kwargs)
        except Exception as exc:
            # Recorded under the same incident kind as a failed post: this
            # writes to a channel the same way, and a correction that never
            # landed has to be visible in the audit log (sca-psr).
            self._audit(
                kind=IncidentKind.POST_FAILED.value, channel=channel, thread_ts=ts,
                sent=0, total=1, error=str(exc),
            )
            raise SlackError(str(exc)) from exc
        return [str(block["type"]) for block in kwargs.get("blocks", [])]
