"""노션 p/ 링크 보정 시험.

케이스 표는 마메치 test_bot.py 의 fix_notion_links 표를 그대로 옮겼다.
보정은 MessagePublisher.post/update 의 발신 단계에서 한다. 마메치 bot.py
post() 와 같은 위치라 대화 답변, 감시 확인 보고, 리뷰 결과, 엔진 실패 본문이
모두 같은 보정을 거친다. 발신 단계 시험이 그 전 경로를 덮는다.
"""

from __future__ import annotations

from typing import Any

import pytest

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.render.blocks import BlockBuilder
from slack_cli_agent.render.markdown import MarkdownConverter
from slack_cli_agent.render.notion_links import fix_notion_links
from slack_cli_agent.render.splitter import ContentSplitter
from slack_cli_agent.render.verifier import SplitVerifier
from slack_cli_agent.slack.publisher import MessagePublisher

CASES = [
    ("id 만 있는 경로",
     "https://app.notion.com/3e2cf5759dc08058a5f9e91c2a3193fe",
     "https://app.notion.com/p/3e2cf5759dc08058a5f9e91c2a3193fe"),
    ("워크스페이스 이름이 붙은 경로",
     "https://app.notion.com/vroong/211cf5759dc080b5878ac92d77315815",
     "https://app.notion.com/p/vroong/211cf5759dc080b5878ac92d77315815"),
    ("하이픈 uuid",
     "https://app.notion.com/3e2cf575-9dc0-8058-a5f9-e91c2a3193fe",
     "https://app.notion.com/p/3e2cf575-9dc0-8058-a5f9-e91c2a3193fe"),
    ("쿼리 문자열",
     "https://app.notion.com/3e2cf5759dc08058a5f9e91c2a3193fe?pvs=204",
     "https://app.notion.com/p/3e2cf5759dc08058a5f9e91c2a3193fe?pvs=204"),
    ("mrkdwn 라벨 링크",
     "<https://app.notion.com/3cfcf5759dc080e7bc3adbea5507541e|요청서>",
     "<https://app.notion.com/p/3cfcf5759dc080e7bc3adbea5507541e|요청서>"),
    ("마크다운 링크",
     "[요청서](https://app.notion.com/3cfcf5759dc080e7bc3adbea5507541e)",
     "[요청서](https://app.notion.com/p/3cfcf5759dc080e7bc3adbea5507541e)"),
    ("이미 p/ 가 있음",
     "https://app.notion.com/p/3e2cf5759dc08058a5f9e91c2a3193fe",
     "https://app.notion.com/p/3e2cf5759dc08058a5f9e91c2a3193fe"),
    ("페이지가 아닌 경로",
     "https://app.notion.com/login",
     "https://app.notion.com/login"),
    ("notion.so 는 대상 아님",
     "https://www.notion.so/3e2cf5759dc08058a5f9e91c2a3193fe",
     "https://www.notion.so/3e2cf5759dc08058a5f9e91c2a3193fe"),
]

BAD = "https://app.notion.com/3e2cf5759dc08058a5f9e91c2a3193fe"
GOOD = "https://app.notion.com/p/3e2cf5759dc08058a5f9e91c2a3193fe"


@pytest.mark.parametrize(("label", "src", "want"), CASES, ids=[c[0] for c in CASES])
def test_fix_notion_links_cases(label: str, src: str, want: str) -> None:
    assert fix_notion_links(src)[0] == want


def test_fix_notion_links_reports_original_url() -> None:
    assert fix_notion_links(BAD)[1] == [BAD]


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


def make_publisher(client: FakeClient) -> MessagePublisher:
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
    )


@pytest.mark.parametrize("rich", [True, False], ids=["rich", "plain"])
def test_post_fixes_notion_link_for_every_caller(rich: bool) -> None:
    # 감시 확인 보고, 리뷰 결과, 엔진 실패 본문은 가드 파이프라인을 거치지 않고
    # publisher.post 로 바로 게시한다. 보정이 여기 있어야 그 경로도 덮인다.
    client = FakeClient()
    make_publisher(client).post("C1", "1.0", f"감시 결과 : [요청서]({BAD})", rich)
    sent = client.sent_text()
    assert GOOD in sent
    assert BAD + ")" not in sent and BAD + "|" not in sent


@pytest.mark.parametrize("rich", [True, False], ids=["rich", "plain"])
def test_update_fixes_notion_link(rich: bool) -> None:
    client = FakeClient()
    make_publisher(client).update("C1", "1.0", f"교정본 [요청서]({BAD})", rich)
    sent = client.sent_text()
    assert GOOD in sent
    assert BAD + ")" not in sent and BAD + "|" not in sent
