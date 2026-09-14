"""MessagePublisher — 발신과 실패 처리.

원본 bot.py 의 `post` 를 재구성했다. 채널별 표기 이원화
(리치 markdown 블록 / 평문 mrkdwn), 분할 점검과 안전 낙하, 리치 표기 거절 시
평문 재발신, 조각 전송 실패 시 부분전달 안내는 원본 로직을 그대로 따른다.

`ELAPSED_MODEL_LINE` 은 원본에서 특정 채널 한정으로 답변 끝에 실행
모델을 적던 자리다. 회사 결합을 걷어내고 `ChannelConfig.rich` 로 일반화했다
— 리치 표기가 켜진 채널에서만 실행 모델을 표기한다. 정규식 자체는 가드
계층이 아니라 `core.markers` 에서 가져온다 — 발신 계층이 가드 계층을
import 할 이유가 없다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.errors import SlackError
from slack_cli_agent.core.markers import ELAPSED_MODEL_LINE
from slack_cli_agent.render.blocks import BlockBuilder
from slack_cli_agent.render.markdown import MarkdownConverter
from slack_cli_agent.render.splitter import ContentSplitter
from slack_cli_agent.render.verifier import SplitVerifier

log = logging.getLogger(__name__)

# 상태를 색으로 가른다. 글을 읽기 전에 성공인지 실패인지 먼저 보이게 한다.
COLOR_FAIL = "#d64541"


class MessagePublisher:
    """채널 표기에 맞춰 발신하고, 실패를 원본과 같은 순서로 처리한다."""

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
        """답변 끝에 실행 모델을 적는다. rich 채널에서만, model 이 있을 때만.

        모델이 앞 대화를 흉내 내 본문 끝에 같은 줄을 써 넣는 경우가 있다.
        붙이기 전에 지운다 — 그대로 두면 두 줄이 되고 값도 서로 다르다.
        """
        if not rich or not model:
            return body
        cleaned = ELAPSED_MODEL_LINE.sub("", body.rstrip()).rstrip()
        return cleaned.rstrip() + f"\n> 실행 모델 : {model}"

    def post(self, channel: str, thread_ts: str, text: str, rich: bool) -> str | None:
        """채널에 맞는 표기로 게시한다.

        리치 자리에서는 마크다운 원문을 markdown 블록으로 그대로 넘긴다.
        그 밖의 자리에서는 mrkdwn 으로 낮춰 쓴다. 채널은 스레드로 답하고
        DM 은 본문에 쓴다.
        """
        if rich:
            separated = self._verifier.separate_tables(text)
            chunks = self._splitter.split_for_blocks(separated)
            problems = self._verifier.verify_chunks(separated, chunks)
            if problems:
                self._audit(
                    kind="split_broken", channel=channel, thread_ts=thread_ts,
                    problems=problems, total=len(text),
                    sizes=[len(c) for c in chunks],
                )
                chunks = self._verifier.safe_fallback(separated)
            # md_chunks 가 표와 문단을 각각의 덩어리로 끊고 split_for_blocks 가
            # 그것들을 다시 이어 붙이면서 사이 빈 줄이 사라진다. 점검이 끝난
            # 뒤에 다시 띄운다.
            chunks = [self._verifier.separate_tables(c) for c in chunks]
        else:
            chunks = self._splitter.chunk(self._markdown.to_mrkdwn(text))

        parent_ts = None if channel.startswith("D") else thread_ts
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
                        kind="blocks_rejected", channel=channel,
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
                    except Exception as retry_exc:  # noqa: BLE001 — 재시도 발송 실패를 최초 예외와 함께 다뤄 실패 보고로 이어간다
                        exc = retry_exc

                self._audit(
                    kind="post_failed", channel=channel, thread_ts=thread_ts,
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
                    except Exception as notify_exc:  # noqa: BLE001 — 부분 발송 실패 안내 자체가 실패해도 원래 오류 보고를 막지 않는다
                        log.warning("부분 발송 실패 안내 전송 실패 : %s", notify_exc)
                raise SlackError(str(exc)) from exc
            sent += 1
            if parent_ts is None:
                parent_ts = res["ts"]

        if rich and len(chunks) > 1:
            self._audit(
                kind="split", channel=channel, thread_ts=thread_ts,
                total=len(text), sizes=[len(c) for c in chunks],
            )
        return parent_ts
