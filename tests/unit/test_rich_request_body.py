"""C2 — 봇이 리치 블록으로 보낸 요청은 미리보기가 아니라 본문 전체를 읽는다.

markdown 블록으로 올린 글의 text 는 알림용 미리보기 한 줄이다. 그대로 넘기면
상대 봇이 보낸 표와 목록이 빠진 토막만 보고 답한다. 마메치 bot.py 는 봇
발신 요청의 text 를 event_body 로 바꾼다. 링크 언퍼얼 폴백도 첨부의 blocks
본문을 text 미리보기보다 먼저 쓴다(bot.py unfurled_message_blocks).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from identity_support import fake_identity

from slack_cli_agent.config.channel import ChannelRegistry
from slack_cli_agent.slack.gate import ResponseGate
from slack_cli_agent.slack.linked_threads import LinkedThreadReader
from slack_cli_agent.slack.listener import EventListener
from slack_cli_agent.slack.transcript import TranscriptRead

MSG = "https://example.slack.com/archives/C0EXAMPLE01/p1788253544408049"


class _Client:
    def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        return {"messages": []}

    def bots_info(self, **kwargs: Any) -> dict[str, Any]:
        return {"bot": {"user_id": "U_PEER"}}


def _listener() -> EventListener:
    return EventListener(
        _Client(), ChannelRegistry(Path("/nonexistent.json")), ResponseGate(),
        identity=fake_identity(),
    )


def _cell(text: str) -> dict[str, Any]:
    return {"type": "rich_text", "elements": [
        {"type": "rich_text_section", "elements": [{"type": "text", "text": text}]}]}


def _rich_body(*, mention: bool) -> list[dict[str, Any]]:
    head: list[dict[str, Any]] = [{"type": "text", "text": "검토 부탁 "}]
    if mention:
        head.append({"type": "user", "user_id": "U_BOT"})
    return [
        {"type": "rich_text", "elements": [
            {"type": "rich_text_section", "elements": head},
            {"type": "rich_text_list", "elements": [
                {"type": "rich_text_section", "elements": [{"type": "text", "text": "첫째 항목"}]},
                {"type": "rich_text_section", "elements": [{"type": "text", "text": "둘째 항목"}]},
            ]},
        ]},
        {"type": "table", "rows": [[_cell("열1"), _cell("열2")], [_cell("값A"), _cell("값B")]]},
    ]


def _bot_event(**extra: Any) -> dict[str, Any]:
    event = {
        "channel": "C1", "ts": "1.0", "bot_id": "B_PEER", "user": "U_PEER",
        "text": "검토 부탁", "blocks": _rich_body(mention=True),
    }
    event.update(extra)
    return event


def _assert_full(text: str) -> None:
    for part in ("첫째 항목", "둘째 항목", "열1", "값B", "<@U_BOT>"):
        assert part in text


class Test봇_리치_요청_본문:
    def test_blocks에만_멘션이_있는_봇_요청은_본문_전체를_넘긴다(self) -> None:
        ctx = _listener().from_message(_bot_event())
        assert ctx is not None
        _assert_full(ctx.text)

    def test_app_mention으로_온_봇_요청도_본문_전체를_넘긴다(self) -> None:
        ctx = _listener().from_app_mention(_bot_event(text="<@U_BOT> 검토 부탁"))
        assert ctx is not None
        _assert_full(ctx.text)

    def test_사람_요청은_text를_그대로_쓴다(self) -> None:
        event = {
            "channel": "C1", "ts": "1.0", "user": "U_HUMAN",
            "text": "<@U_BOT> *굵게* 원문", "blocks": _rich_body(mention=True),
        }
        ctx = _listener().from_app_mention(event)
        assert ctx is not None
        assert ctx.text == "<@U_BOT> *굵게* 원문"

    def test_blocks가_없는_봇_요청은_text를_쓴다(self) -> None:
        ctx = _listener().from_app_mention(
            {"channel": "C1", "ts": "1.0", "bot_id": "B_PEER", "text": "<@U_BOT> 평문"}
        )
        assert ctx is not None
        assert ctx.text == "<@U_BOT> 평문"


class _Failing:
    def read_thread(self, channel: str, thread_ts: str, before_ts: object = None) -> TranscriptRead:
        return TranscriptRead(body="", read_ok=False)


def _reader() -> LinkedThreadReader:
    return LinkedThreadReader(transcript=_Failing(), channel_name=lambda c: "테스트", max_links=3)


class Test언퍼얼_폴백_본문:
    def test_첨부의_blocks_본문을_미리보기보다_먼저_쓴다(self) -> None:
        att = [{"is_msg_unfurl": True, "from_url": MSG, "text": "검토 부탁",
                "blocks": _rich_body(mention=False)}]
        body = _reader().of(MSG, self_channel="C099", attachments=att)[0].body
        for part in ("첫째 항목", "둘째 항목", "열1", "값B"):
            assert part in body

    def test_message_blocks에_실린_본문도_쓴다(self) -> None:
        att = [{"is_msg_unfurl": True, "from_url": MSG, "text": "검토 부탁",
                "message_blocks": [{"message": {"blocks": _rich_body(mention=False)}}]}]
        body = _reader().of(MSG, self_channel="C099", attachments=att)[0].body
        assert "둘째 항목" in body and "값B" in body

    def test_blocks가_없으면_text를_쓴다(self) -> None:
        att = [{"is_msg_unfurl": True, "from_url": MSG, "text": "평문 본문"}]
        assert _reader().of(MSG, self_channel="C099", attachments=att)[0].body == "평문 본문"
