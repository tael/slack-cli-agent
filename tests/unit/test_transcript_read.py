"""조회 실패와 옮길 메시지가 없는 것을 구분하는 경로(sca-678).

thread_transcript 는 둘 다 빈 문자열을 낸다. 그 구분이 필요한 쪽은
read_thread 를 쓴다.
"""

from __future__ import annotations

from typing import Any

from identity_support import fake_identity

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.observability.notices import NoticeCatalog
from slack_cli_agent.slack.transcript import TranscriptBuilder


class 대역클라이언트:
    def __init__(self, messages: list[dict[str, Any]] | None = None, *, 터진다: bool = False) -> None:
        self._messages = messages or []
        self._터진다 = 터진다

    def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        if self._터진다:
            raise RuntimeError("슬랙 조회 실패")
        return {"messages": self._messages}

    def conversations_history(self, **kwargs: Any) -> dict[str, Any]:
        return self.conversations_replies(**kwargs)


def 만들기(client: 대역클라이언트) -> TranscriptBuilder:
    return TranscriptBuilder(
        client,
        RuntimeSettings(),
        NoticeCatalog(),
        name_resolver=lambda uid: {"U1": "김철수"}.get(uid, ""),
        identity=fake_identity(),
        bot_display_name="테스트봇",
    )


class Test조회결과구분:
    def test_조회가_실패하면_read_ok_가_거짓이다(self) -> None:
        read = 만들기(대역클라이언트(터진다=True)).read_thread("C1", "1700000000.000001", None)
        assert read.body == ""
        assert read.read_ok is False

    def test_옮길_메시지가_없어도_조회는_성공이다(self) -> None:
        read = 만들기(대역클라이언트([])).read_thread("C1", "1700000000.000001", None)
        assert read.body == ""
        assert read.read_ok is True

    def test_제외_규칙으로_다_빠져도_조회는_성공이다(self) -> None:
        """조회는 됐고 기록에 넣을 말만 없는 경우다."""
        messages = [{"ts": "1700000000.000001", "user": "U1", "text": "   "}]
        read = 만들기(대역클라이언트(messages)).read_thread("C1", "1700000000.000001", None)
        assert read.body == ""
        assert read.read_ok is True

    def test_읽은_본문은_read_ok_와_함께_온다(self) -> None:
        messages = [{"ts": "1700000000.000001", "user": "U1", "text": "안녕하세요"}]
        read = 만들기(대역클라이언트(messages)).read_thread("C1", "1700000000.000001", None)
        assert "안녕하세요" in read.body
        assert read.read_ok is True

    def test_thread_transcript_는_같은_본문을_그대로_낸다(self) -> None:
        messages = [{"ts": "1700000000.000001", "user": "U1", "text": "안녕하세요"}]
        builder = 만들기(대역클라이언트(messages))
        assert builder.thread_transcript("C1", "1700000000.000001", None) == builder.read_thread(
            "C1", "1700000000.000001", None
        ).body
