"""C1 — 리치 본문(blocks) 안의 멘션 인식.

markdown 블록으로 올린 글은 text 에 알림용 미리보기만 남고 본문은 blocks 에
있다. 멘션도 blocks 안에서 user 요소로 쪼개져 들어간다. 그 멘션이 미리보기에
없으면 슬랙이 app_mention 을 보내지 않으므로 message 이벤트에서 blocks 를
훑어 접수해야 한다. 마메치 bot.py block_text, event_body, addressed_by_bot 상당.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from identity_support import fake_identity

from slack_cli_agent.config.channel import ChannelRegistry
from slack_cli_agent.guard.mentions import PeerExchangeLimiter
from slack_cli_agent.slack.blocks import block_text, event_body
from slack_cli_agent.slack.gate import ResponseGate
from slack_cli_agent.slack.listener import EventListener


class _Client:
    def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        return {"messages": []}

    def bots_info(self, **kwargs: Any) -> dict[str, Any]:
        return {"bot": {"user_id": "U_PEER"}}


def _listener(**kwargs: Any) -> EventListener:
    return EventListener(
        _Client(), ChannelRegistry(Path("/nonexistent.json")), ResponseGate(),
        identity=fake_identity(), **kwargs,
    )


def _rich(*elements: dict[str, Any]) -> list[dict[str, Any]]:
    return [{
        "type": "rich_text",
        "elements": [{"type": "rich_text_section", "elements": list(elements)}],
    }]


_USER_SELF = {"type": "user", "user_id": "U_BOT"}


class Test블록_평문화:
    def test_user_요소를_멘션_문자열로_바꾼다(self) -> None:
        blocks = _rich({"type": "text", "text": "검토 "}, _USER_SELF)
        assert "<@U_BOT>" in event_body({"blocks": blocks})

    def test_section_fields_안의_멘션도_찾는다(self) -> None:
        block = {
            "type": "section",
            "fields": [{"type": "mrkdwn", "text": "담당 <@U_BOT>"}],
        }
        assert "<@U_BOT>" in block_text(block)

    def test_중첩된_목록과_인용_안의_멘션도_찾는다(self) -> None:
        blocks = [{
            "type": "rich_text",
            "elements": [{
                "type": "rich_text_list",
                "elements": [{
                    "type": "rich_text_section",
                    "elements": [{"type": "text", "text": "항목 "}, _USER_SELF],
                }],
            }],
        }]
        assert "<@U_BOT>" in event_body({"blocks": blocks})

    def test_표_rows_안의_멘션도_찾는다(self) -> None:
        block = {"type": "table", "rows": [[{"type": "rich_text", "elements": [
            {"type": "rich_text_section", "elements": [_USER_SELF]}]}]]}
        assert "<@U_BOT>" in block_text(block)

    def test_blocks가_없으면_text를_쓴다(self) -> None:
        assert event_body({"text": "미리보기"}) == "미리보기"

    def test_이상한_노드는_무시한다(self) -> None:
        assert block_text([None, 3, "x", {"type": "divider"}]) == ""


class Test메시지_이벤트_blocks_멘션_접수:
    def test_봇이_blocks로만_나를_부르면_받는다(self) -> None:
        event = {
            "type": "message", "channel": "C1", "bot_id": "B_PEER", "ts": "2.0",
            "thread_ts": "1.0", "text": "검토 요청드립니다",
            "blocks": _rich({"type": "text", "text": "검토 요청 "}, _USER_SELF),
        }
        ctx = _listener().from_message(event)
        assert ctx is not None
        assert ctx.unaddressed is False
        assert ctx.user == "U_PEER"
        assert ctx.thread_ts == "1.0"

    def test_사람이_blocks로만_나를_부르면_받는다(self) -> None:
        event = {
            "type": "message", "channel": "C1", "user": "U1", "ts": "2.0",
            "text": "봐주세요", "blocks": _rich(_USER_SELF),
        }
        ctx = _listener().from_message(event)
        assert ctx is not None
        assert ctx.unaddressed is False

    def test_text에도_멘션이_있으면_app_mention에_맡긴다(self) -> None:
        event = {
            "type": "message", "channel": "C1", "bot_id": "B_PEER", "ts": "2.0",
            "thread_ts": "1.0", "text": "<@U_BOT> 검토", "blocks": _rich(_USER_SELF),
        }
        assert _listener().from_message(event) is None

    def test_blocks에도_멘션이_없는_봇_말은_거른다(self) -> None:
        event = {
            "type": "message", "channel": "C1", "bot_id": "B_PEER", "ts": "2.0",
            "thread_ts": "1.0", "text": "혼잣말",
            "blocks": _rich({"type": "text", "text": "혼잣말"}),
        }
        assert _listener().from_message(event) is None

    def test_내_메시지는_blocks에_내_멘션이_있어도_거른다(self) -> None:
        event = {
            "type": "message", "channel": "C1", "bot_id": "B_BOT", "ts": "2.0",
            "thread_ts": "1.0", "text": "요약", "blocks": _rich(_USER_SELF),
        }
        assert _listener().from_message(event) is None

    def test_편집_이벤트는_거른다(self) -> None:
        event = {
            "type": "message", "subtype": "message_changed", "channel": "C1",
            "bot_id": "B_PEER", "ts": "2.0", "text": "", "blocks": _rich(_USER_SELF),
        }
        assert _listener().from_message(event) is None

    def test_봇간_왕복_상한이_걸린다(self) -> None:
        limiter = PeerExchangeLimiter(limit=1)
        listener = _listener(peer_limiter=limiter)
        event = {
            "type": "message", "channel": "C1", "bot_id": "B_PEER", "ts": "2.0",
            "thread_ts": "1.0", "text": "검토", "blocks": _rich(_USER_SELF),
        }
        assert listener.from_message(event) is not None
        assert listener.from_message({**event, "ts": "3.0"}) is None


class Test앱_멘션_봇_판정:
    def test_봇이_보낸_app_mention은_blocks의_멘션으로도_판정한다(self) -> None:
        event = {
            "channel": "C1", "bot_id": "B_PEER", "ts": "1.0",
            "text": "미리보기", "blocks": _rich(_USER_SELF),
        }
        assert _listener().from_app_mention(event) is not None
