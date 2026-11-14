"""스레드 답글이 한 페이지를 넘으면 cursor 로 끝까지 모은다 (F17).

마메치 대조: 50개(한 페이지)를 넘는 답글이 유실되던 결함. 슬랙
conversations_replies 는 답글을 오래된 순으로 한 페이지씩 돌려주므로,
has_more/next_cursor 를 따라가지 않으면 가장 최근 답글이 통째로 빠진다.
"""

from __future__ import annotations

from typing import Any

from identity_support import fake_identity

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.observability.notices import NoticeCatalog
from slack_cli_agent.slack.transcript import TranscriptBuilder


class 페이지클라이언트:
    """cursor 를 따라 여러 페이지를 돌려주는 대역."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages
        self.calls: list[str] = []

    def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        cursor = kwargs.get("cursor", "")
        self.calls.append(cursor)
        for page in self._pages:
            if page.get("_cursor", "") == cursor:
                return {k: v for k, v in page.items() if not k.startswith("_")}
        return {"messages": []}

    def conversations_history(self, **kwargs: Any) -> dict[str, Any]:
        return self.conversations_replies(**kwargs)


def 만들기(client: 페이지클라이언트, **settings: Any) -> TranscriptBuilder:
    return TranscriptBuilder(
        client,
        RuntimeSettings(**settings),
        NoticeCatalog(),
        name_resolver=lambda uid: {"U1": "김철수"}.get(uid, ""),
        identity=fake_identity(),
        bot_display_name="테스트봇",
    )


def _msg(ts: str, text: str) -> dict[str, Any]:
    return {"ts": ts, "user": "U1", "text": text}


class Test답글페이지네이션:
    def test_여러_페이지_답글이_모두_본문에_들어간다(self) -> None:
        pages = [
            {
                "_cursor": "",
                "messages": [_msg("1700000000.000001", "첫째줄")],
                "has_more": True,
                "response_metadata": {"next_cursor": "c2"},
            },
            {
                "_cursor": "c2",
                "messages": [_msg("1700000000.000002", "둘째줄")],
                "has_more": True,
                "response_metadata": {"next_cursor": "c3"},
            },
            {
                "_cursor": "c3",
                "messages": [_msg("1700000000.000003", "마지막줄")],
                "has_more": False,
            },
        ]
        client = 페이지클라이언트(pages)
        read = 만들기(client).read_thread("C1", "1700000000.000001", None)
        assert read.read_ok is True
        assert "첫째줄" in read.body
        assert "둘째줄" in read.body
        assert "마지막줄" in read.body
        assert client.calls == ["", "c2", "c3"]

    def test_페이지_상한을_넘으면_멈춘다(self) -> None:
        # has_more 가 계속 True 여도 history_max_pages 만큼만 조회한다.
        pages = [
            {
                "_cursor": "",
                "messages": [_msg("1700000000.000001", "무한")],
                "has_more": True,
                "response_metadata": {"next_cursor": ""},
            }
        ]
        client = 페이지클라이언트(pages)
        read = 만들기(client, history_max_pages=3).read_thread("C1", "1700000000.000001", None)
        # next_cursor 가 빈 문자열이면 has_more 여도 더 못 돌므로 1회에 멈춘다.
        assert read.read_ok is True
        assert len(client.calls) == 1

    def test_한_페이지면_기존처럼_한_번만_조회한다(self) -> None:
        pages = [{"_cursor": "", "messages": [_msg("1700000000.000001", "안녕")]}]
        client = 페이지클라이언트(pages)
        read = 만들기(client).read_thread("C1", "1700000000.000001", None)
        assert "안녕" in read.body
        assert client.calls == [""]
