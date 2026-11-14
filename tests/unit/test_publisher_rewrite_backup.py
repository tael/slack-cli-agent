"""리치 메시지 수정 시 원문 백업 + 수정 후 재조회 검증 (J7).

기대값은 마메치 post_rich.py 의 backup_original / verify / separate_blocks 를
실제로 읽고 그 동작을 그대로 옮겨 만들었다.
- backup_original: chat.update 전에 현재 본문(markdown 블록 text 우선, 없으면
  message text)을 상태 디렉터리에 파일로 남기고, 조회 실패면 고치지 않고 멈춘다.
- verify: 수정 후 메시지를 다시 읽어 저장된 블록 타입을 확인한다. 헤딩·표·구분선을
  적었는데 rich_text 하나만 남았으면 변환이 걸리지 않은 것으로 본다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.errors import SlackError
from slack_cli_agent.observability.audit import IncidentKind
from slack_cli_agent.render.blocks import BlockBuilder
from slack_cli_agent.render.markdown import MarkdownConverter
from slack_cli_agent.render.splitter import ContentSplitter
from slack_cli_agent.render.verifier import SplitVerifier
from slack_cli_agent.slack.publisher import MessagePublisher


class FakeClient:
    """replies/history 응답을 큐에서 꺼내 돌려주는 대역."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._replies: list[dict] = []
        self._history: list[dict] = []

    def queue_replies(self, response: dict) -> None:
        self._replies.append(response)

    def queue_history(self, response: dict) -> None:
        self._history.append(response)

    def conversations_replies(self, **kwargs: Any) -> dict:
        self.calls.append(("conversations_replies", kwargs))
        return self._replies.pop(0) if self._replies else {"messages": []}

    def conversations_history(self, **kwargs: Any) -> dict:
        self.calls.append(("conversations_history", kwargs))
        return self._history.pop(0) if self._history else {"messages": []}

    def chat_update(self, **kwargs: Any) -> dict:
        self.calls.append(("chat_update", kwargs))
        return {"ok": True}

    def kinds(self) -> list[str]:
        return [name for name, _ in self.calls]


def make_publisher(
    client: FakeClient, backup_dir: Path | None, audit: list | None = None
) -> MessagePublisher:
    settings = RuntimeSettings()
    blocks = BlockBuilder("테스트봇")
    sink = None
    if audit is not None:
        def sink(**fields: Any) -> None:
            audit.append(fields)
    clock = iter([1700000000.0, 1700000001.0, 1700000002.0]).__next__
    return MessagePublisher(
        client=client,
        settings=settings,
        markdown=MarkdownConverter(),
        splitter=ContentSplitter(settings, blocks),
        verifier=SplitVerifier(settings),
        blocks=blocks,
        bot_display_name="테스트봇",
        audit=sink,
        backup_dir=backup_dir,
        clock=clock,
    )


def _msg(ts: str, *, markdown: str | None = None, text: str = "", blocks: list | None = None) -> dict:
    out: dict[str, Any] = {"ts": ts, "text": text}
    if markdown is not None:
        out["blocks"] = [{"type": "markdown", "text": markdown}]
    if blocks is not None:
        out["blocks"] = blocks
    return out


class TestRewriteBackup:
    def test_수정전_원문을_상태디렉터리에_백업한다(self, tmp_path: Path) -> None:
        client = FakeClient()
        client.queue_replies({"messages": [_msg("100.0", markdown="원래 본문")]})  # backup
        client.queue_replies({"messages": [_msg("100.0", blocks=[{"type": "rich_text"}])]})  # verify
        pub = make_publisher(client, tmp_path)

        pub.update("C1", "100.0", "고친 본문", rich=True)

        saved = list(tmp_path.glob("C1_100.0_*.md"))
        assert len(saved) == 1
        assert saved[0].read_text() == "원래 본문"
        assert "chat_update" in client.kinds()
        # 백업은 chat_update 앞에 일어난다.
        assert client.kinds().index("conversations_replies") < client.kinds().index("chat_update")

    def test_markdown블록이_없으면_message_text를_백업한다(self, tmp_path: Path) -> None:
        client = FakeClient()
        client.queue_replies({"messages": [_msg("100.0", text="평문 원문")]})
        client.queue_replies({"messages": [_msg("100.0", blocks=[{"type": "rich_text"}])]})
        pub = make_publisher(client, tmp_path)

        pub.update("C1", "100.0", "고침", rich=True)

        saved = list(tmp_path.glob("C1_100.0_*.md"))
        assert saved[0].read_text() == "평문 원문"

    def test_replies가_비면_history로_원문을_찾는다(self, tmp_path: Path) -> None:
        client = FakeClient()
        client.queue_replies({"messages": []})  # replies miss
        client.queue_history({"messages": [_msg("100.0", markdown="기록 본문")]})  # history hit
        client.queue_replies({"messages": [_msg("100.0", blocks=[{"type": "rich_text"}])]})
        pub = make_publisher(client, tmp_path)

        pub.update("C1", "100.0", "고침", rich=True)

        saved = list(tmp_path.glob("C1_100.0_*.md"))
        assert saved[0].read_text() == "기록 본문"

    def test_원문조회_실패시_고치지_않고_멈춘다(self, tmp_path: Path) -> None:
        client = FakeClient()
        client.queue_replies({"messages": []})
        client.queue_history({"messages": []})
        pub = make_publisher(client, tmp_path)

        with pytest.raises(SlackError):
            pub.update("C1", "100.0", "고침", rich=True)

        assert "chat_update" not in client.kinds()
        assert list(tmp_path.glob("*.md")) == []

    def test_수정후_rich_text만_남으면_경고를_기록한다(self, tmp_path: Path) -> None:
        audit: list[dict] = []
        client = FakeClient()
        client.queue_replies({"messages": [_msg("100.0", markdown="원문")]})
        # 헤딩을 적었는데 저장은 rich_text 하나뿐 — 변환 실패.
        client.queue_replies({"messages": [_msg("100.0", blocks=[{"type": "rich_text"}])]})
        pub = make_publisher(client, tmp_path, audit=audit)

        pub.update("C1", "100.0", "## 제목\n\n본문", rich=True)

        kinds = [a.get("kind") for a in audit]
        assert IncidentKind.REWRITE_UNCONVERTED.value in kinds

    def test_변환이_정상이면_경고하지_않는다(self, tmp_path: Path) -> None:
        audit: list[dict] = []
        client = FakeClient()
        client.queue_replies({"messages": [_msg("100.0", markdown="원문")]})
        client.queue_replies(
            {"messages": [_msg("100.0", blocks=[{"type": "header"}, {"type": "rich_text"}])]}
        )
        pub = make_publisher(client, tmp_path, audit=audit)

        pub.update("C1", "100.0", "## 제목\n\n본문", rich=True)

        kinds = [a.get("kind") for a in audit]
        assert IncidentKind.REWRITE_UNCONVERTED.value not in kinds

    def test_블록표기_없는_본문은_rich_text만_남아도_정상이다(self, tmp_path: Path) -> None:
        audit: list[dict] = []
        client = FakeClient()
        client.queue_replies({"messages": [_msg("100.0", markdown="원문")]})
        client.queue_replies({"messages": [_msg("100.0", blocks=[{"type": "rich_text"}])]})
        pub = make_publisher(client, tmp_path, audit=audit)

        # 헤딩·표·구분선이 없으면 rich_text 하나가 정상.
        pub.update("C1", "100.0", "그냥 **굵은** 한 문단", rich=True)

        kinds = [a.get("kind") for a in audit]
        assert IncidentKind.REWRITE_UNCONVERTED.value not in kinds

    def test_backup_dir_미설정이면_백업도_검증도_생략한다(self, tmp_path: Path) -> None:
        client = FakeClient()
        pub = make_publisher(client, backup_dir=None)

        result = pub.update("C1", "100.0", "## 제목\n\n본문", rich=True)

        assert client.kinds() == ["chat_update"]
        assert "markdown" in result
        assert list(tmp_path.glob("*.md")) == []
