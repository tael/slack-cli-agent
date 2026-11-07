"""게시 본문의 비밀값 마스킹 (A17).

엔진 답변이 셸 출력이나 설정 파일을 인용하면 토큰 원문이 그대로 슬랙에 게시된다.
기존 core.secrets 는 프로필·경로 검사에만 쓰였고 게시 경로에는 연결돼 있지 않았다.
마메치 bot.py 에도 게시 본문 마스킹이 없어 옮길 원본은 없다. 결함을 옮기지 않고
게시 경로 한곳(MessagePublisher.post, update)에서 가린다.
"""

from __future__ import annotations

from typing import Any

import pytest

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.secrets import OUTBOUND_REDACTED as REDACTED, redact_outbound
from slack_cli_agent.render.blocks import BlockBuilder
from slack_cli_agent.render.markdown import MarkdownConverter
from slack_cli_agent.render.splitter import ContentSplitter
from slack_cli_agent.render.verifier import SplitVerifier
from slack_cli_agent.slack.publisher import MessagePublisher

# 가짜 값이다. 원문 그대로 두면 GitHub 푸시 보호가 실제 토큰으로 보고 거부하므로
# 접두와 본문을 실행 시점에 잇는다.
SLACK_BOT = "-".join(["xoxb", "1234567890", "0987654321", "AbCdEfGhIjKlMnOpQrStUvWx"])
SLACK_XOXC = "-".join(["xoxc", "1111111111", "2222222222", "3333333333", "abcdef0123456789"])
SLACK_XOXD = "xoxd" + "-AbCdEf%2BGhIjKl%2FMnOpQrStUv"
APP_TOKEN = "-".join(["xapp", "1", "A0123456789", "1234567890123", "abcdef0123456789abcdef"])
AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
GITHUB = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
GITHUB_PAT = "github_pat_" + "11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz0123456789ABCD"
ANTHROPIC = "sk-ant-" + "api03-" + "x" * 40
OPENAI = "sk-proj-" + "Ab1" * 12
PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\nabcdef\n-----END RSA PRIVATE KEY-----"


@pytest.mark.parametrize(
    "secret", [SLACK_BOT, SLACK_XOXC, SLACK_XOXD, APP_TOKEN, AWS_KEY, GITHUB, GITHUB_PAT, ANTHROPIC, OPENAI]
)
def test_토큰_형태_문자열을_가린다(secret: str) -> None:
    text = f"헤더 Authorization: Bearer {secret} 로 호출했다"
    out = redact_outbound(text)
    assert secret not in out
    assert REDACTED in out
    assert out.startswith("헤더 Authorization: Bearer ")
    assert out.endswith(" 로 호출했다")


def test_개인키_블록을_통째로_가린다() -> None:
    out = redact_outbound(f"키 파일 내용:\n```\n{PEM}\n```\n끝")
    assert "MIIEowIBAAKCAQEA" not in out
    assert "PRIVATE KEY" not in out
    assert out.endswith("```\n끝")


@pytest.mark.parametrize(
    "text",
    [
        "`SLACK_MCP_XOXC_TOKEN` 과 `SLACK_MCP_XOXD_TOKEN` 을 함께 보낸다",
        "xoxc 토큰과 xoxd 쿠키, xoxb-... 형태",
        "xoxc-토큰은 만료가 있다",
        "sk-learn 과 task-ask 는 패키지 이름이다",
        "AKIA 로 시작하는 키",
        "오더번호 20260601123456#0001, https://example.com/a?b=c",
        "ghp_ 접두가 붙은 토큰",
    ],
)
def test_토큰이_아닌_설명문은_그대로_둔다(text: str) -> None:
    assert redact_outbound(text) == text


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def chat_postMessage(self, **kwargs: Any) -> dict:
        self.calls.append(("chat_postMessage", kwargs))
        return {"ok": True, "ts": "1700000000.000100"}

    def chat_update(self, **kwargs: Any) -> dict:
        self.calls.append(("chat_update", kwargs))
        return {"ok": True}

    def files_upload_v2(self, **kwargs: Any) -> dict:
        self.calls.append(("files_upload_v2", kwargs))
        return {"ok": True}


def _publisher(client: FakeClient, audit: list | None = None) -> MessagePublisher:
    settings = RuntimeSettings()
    blocks = BlockBuilder("테스트봇")

    def sink(**fields: Any) -> None:
        if audit is not None:
            audit.append(fields)

    return MessagePublisher(
        client=client,
        settings=settings,
        markdown=MarkdownConverter(),
        splitter=ContentSplitter(settings, blocks),
        verifier=SplitVerifier(settings),
        blocks=blocks,
        bot_display_name="테스트봇",
        audit=sink,
    )


def _sent_text(client: FakeClient) -> str:
    return repr([kwargs for _, kwargs in client.calls])


@pytest.mark.parametrize("rich", [True, False])
def test_게시_본문에서_토큰이_가려진다(rich: bool) -> None:
    client = FakeClient()
    _publisher(client).post("C1", "1.0", f"설정 값은 {SLACK_XOXC} 이고 키는 {AWS_KEY} 다", rich)
    sent = _sent_text(client)
    assert SLACK_XOXC not in sent
    assert AWS_KEY not in sent
    assert REDACTED in sent


def test_csv_첨부_내용에서도_토큰이_가려진다() -> None:
    client = FakeClient()
    _publisher(client).post("C1", "1.0", f"표:\n```csv\nname,token\nbot,{SLACK_BOT}\n```", True)
    uploads = [kwargs for name, kwargs in client.calls if name == "files_upload_v2"]
    assert uploads
    assert SLACK_BOT.encode() not in uploads[0]["content"]
    assert SLACK_BOT not in _sent_text(client)


def test_교정본_갱신에서도_토큰이_가려진다() -> None:
    client = FakeClient()
    _publisher(client).update("C1", "1.0", f"고친 본문 {GITHUB}", False)
    sent = _sent_text(client)
    assert GITHUB not in sent
    assert REDACTED in sent


def test_가린_사실을_감사_기록에_남기되_원문은_남기지_않는다() -> None:
    client = FakeClient()
    audit: list = []
    _publisher(client, audit).post("C1", "1.0", f"값 {SLACK_BOT}", False)
    hits = [row for row in audit if row.get("kind") == "secret_redacted"]
    assert len(hits) == 1
    assert hits[0]["count"] == 1
    assert SLACK_BOT not in repr(audit)


def test_토큰이_없으면_감사_기록을_남기지_않는다() -> None:
    client = FakeClient()
    audit: list = []
    _publisher(client, audit).post("C1", "1.0", "평범한 답변", False)
    assert not [row for row in audit if row.get("kind") == "secret_redacted"]
