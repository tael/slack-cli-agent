"""노션 p/ 링크 보정 시험.

케이스 표는 마메치 test_bot.py 의 fix_notion_links 표를 그대로 옮겼다.
"""

from __future__ import annotations

import pytest

from slack_cli_agent.guard.base import GuardContext
from slack_cli_agent.guard.links import NotionLinkGuard, fix_notion_links

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


@pytest.mark.parametrize(("label", "src", "want"), CASES, ids=[c[0] for c in CASES])
def test_fix_notion_links_cases(label: str, src: str, want: str) -> None:
    assert fix_notion_links(src)[0] == want


def test_fix_notion_links_reports_original_url() -> None:
    url = "https://app.notion.com/3e2cf5759dc08058a5f9e91c2a3193fe"
    assert fix_notion_links(url)[1] == [url]


def test_guard_fixes_link_in_body() -> None:
    body = "요청서는 https://app.notion.com/3e2cf5759dc08058a5f9e91c2a3193fe 에 있습니다."
    result = NotionLinkGuard().apply(body, GuardContext())
    assert result.changed is True
    assert "https://app.notion.com/p/3e2cf5759dc08058a5f9e91c2a3193fe" in result.body
    assert result.detail == {"fixed": ["https://app.notion.com/3e2cf5759dc08058a5f9e91c2a3193fe"]}


def test_guard_leaves_body_without_bad_link() -> None:
    body = "링크 없음. https://app.notion.com/login"
    result = NotionLinkGuard().apply(body, GuardContext())
    assert result.changed is False
    assert result.body == body


def test_guard_is_registered_in_application_pipeline() -> None:
    import inspect

    from slack_cli_agent.core import application

    assert "NotionLinkGuard()" in inspect.getsource(application.Application._guards)
