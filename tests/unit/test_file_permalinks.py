"""슬랙 파일 permalink 제거 (D4, 마메치 bot.py strip_file_permalinks).

본문에 슬랙 파일 링크가 있으면 슬랙이 그 파일을 메시지에 다시 붙인다.
unfurl 을 꺼도 같다. 업로드한 파일 카드가 스레드에 두 번 나가지 않도록
게시 직전에 링크 주소를 떼고 링크 글자만 남긴다.
"""

from __future__ import annotations

from typing import Any

import pytest

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.observability.audit import IncidentKind
from slack_cli_agent.render.blocks import BlockBuilder
from slack_cli_agent.render.file_links import strip_file_permalinks
from slack_cli_agent.render.markdown import MarkdownConverter
from slack_cli_agent.render.splitter import ContentSplitter
from slack_cli_agent.render.verifier import SplitVerifier
from slack_cli_agent.slack.publisher import MessagePublisher

FILE_URL = "https://vroongcorp.slack.com/files/U0ACANBHY3U/F0ABCDEF123/report.pdf"
MSG_URL = "https://vroongcorp.slack.com/archives/C0CBQA67AAH/p1785288263423719"

CASES = [
    ("markdown 링크", f"보고서 : [report.pdf]({FILE_URL}) 입니다", "보고서 : report.pdf 입니다"),
    ("꺾쇠 markdown 링크", f"[보고서](<{FILE_URL}>) 첨부", "보고서 첨부"),
    ("mrkdwn 라벨 링크", f"<{FILE_URL}|report.pdf> 첨부", "report.pdf 첨부"),
    ("mrkdwn 맨 링크", f"<{FILE_URL}> 첨부", " 첨부"),
    ("맨 주소", f"파일 : {FILE_URL}\n다음 줄", "파일 : \n다음 줄"),
    ("메시지 permalink 유지", f"원문 : {MSG_URL}", f"원문 : {MSG_URL}"),
    ("파일 아닌 링크 유지", "[문서](https://example.com/files/a.pdf)", "[문서](https://example.com/files/a.pdf)"),
    ("빈 본문", "", ""),
]


@pytest.mark.parametrize(("label", "src", "want"), CASES, ids=[c[0] for c in CASES])
def test_strip_file_permalinks_cases(label: str, src: str, want: str) -> None:
    assert strip_file_permalinks(src) == want


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def chat_postMessage(self, **kwargs: Any) -> dict:
        self.calls.append(("chat_postMessage", kwargs))
        return {"ok": True, "ts": "1700000000.000100"}

    def chat_update(self, **kwargs: Any) -> dict:
        self.calls.append(("chat_update", kwargs))
        return {"ok": True}

    def sent_text(self) -> str:
        return repr([kwargs for _, kwargs in self.calls])


def make_publisher(client: FakeClient, audits: list[dict]) -> MessagePublisher:
    settings = RuntimeSettings()
    blocks = BlockBuilder("테스트봇")
    return MessagePublisher(
        client=client,
        settings=settings,
        markdown=MarkdownConverter(),
        splitter=ContentSplitter(settings, blocks),
        verifier=SplitVerifier(settings),
        blocks=blocks,
        bot_display_name="테스트봇",
        audit=lambda **kw: audits.append(kw),
    )


@pytest.mark.parametrize("rich", [True, False], ids=["rich", "plain"])
def test_post_strips_file_permalink(rich: bool) -> None:
    client, audits = FakeClient(), []
    make_publisher(client, audits).post("C1", "1.0", f"[report.pdf]({FILE_URL}) 를 올렸다", rich)
    sent = client.sent_text()
    assert "/files/" not in sent
    assert "report.pdf" in sent
    assert [a["kind"] for a in audits] == [IncidentKind.FILE_PERMALINK_STRIPPED.value]


@pytest.mark.parametrize("rich", [True, False], ids=["rich", "plain"])
def test_update_strips_file_permalink(rich: bool) -> None:
    client, audits = FakeClient(), []
    make_publisher(client, audits).update("C1", "1.0", f"교정본 <{FILE_URL}|report.pdf>", rich)
    assert "/files/" not in client.sent_text()
    assert [a["kind"] for a in audits] == [IncidentKind.FILE_PERMALINK_STRIPPED.value]


def test_post_without_file_link_records_nothing() -> None:
    client, audits = FakeClient(), []
    make_publisher(client, audits).post("C1", "1.0", f"원문 {MSG_URL}", True)
    assert MSG_URL in client.sent_text()
    assert audits == []


@pytest.mark.parametrize("method", ["post", "update"])
def test_redaction_runs_before_file_link_strip(method: str) -> None:
    """A17 비밀값 마스킹이 먼저, 파일 링크 제거가 다음이다. 두 감사가 그 순서로 남는다."""
    token = "-".join(["xoxb", "1234567890", "0987654321", "AbCdEfGhIjKlMnOpQrStUvWx"])
    client, audits = FakeClient(), []
    getattr(make_publisher(client, audits), method)("C1", "1.0", f"값 {token} 파일 <{FILE_URL}|a.pdf>", False)
    sent = client.sent_text()
    assert token not in sent
    assert "/files/" not in sent
    assert [a["kind"] for a in audits] == [
        IncidentKind.SECRET_REDACTED.value,
        IncidentKind.FILE_PERMALINK_STRIPPED.value,
    ]


@pytest.mark.parametrize("rich", [True, False], ids=["rich", "plain"])
def test_link_only_body_keeps_link_instead_of_posting_nothing(rich: bool) -> None:
    """링크만 있는 답을 떼면 본문이 비어 아무것도 안 올라간다. 그때는 링크를 남긴다."""
    client, audits = FakeClient(), []
    ts = make_publisher(client, audits).post("C1", "1.0", f"<{FILE_URL}>", rich)
    assert ts
    assert "/files/" in client.sent_text()
    assert audits == []


def test_link_only_correction_keeps_link() -> None:
    client, audits = FakeClient(), []
    make_publisher(client, audits).update("C1", "1.0", FILE_URL, False)
    assert "/files/" in client.sent_text()
    assert audits == []


def test_link_only_body_with_model_footer_keeps_link() -> None:
    """rich 채널 본 답변은 게시 전에 실행 모델 꼬리 줄이 붙는다. 꼬리 줄만 남는 것도 빈 본문으로 본다."""
    client, audits = FakeClient(), []
    publisher = make_publisher(client, audits)
    body = publisher.apply_elapsed_model_line(f"<{FILE_URL}>", "gpt-5", True)
    assert publisher.post("C1", "1.0", body, True)
    assert "/files/" in client.sent_text()
    assert audits == []


def test_link_with_text_and_model_footer_still_strips() -> None:
    client, audits = FakeClient(), []
    publisher = make_publisher(client, audits)
    body = publisher.apply_elapsed_model_line(f"보고서 : <{FILE_URL}|report.pdf>", "gpt-5", True)
    publisher.post("C1", "1.0", body, True)
    assert "/files/" not in client.sent_text()
    assert [a["kind"] for a in audits] == [IncidentKind.FILE_PERMALINK_STRIPPED.value]
