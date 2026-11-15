"""프론트매터 파서. 외부 yaml 의존 없이 쓰는 최소 형식만 받는다."""

from __future__ import annotations

import pytest

from slack_cli_agent.registry.frontmatter import FrontmatterError, parse_document


def test_문자열_불리언_인라인목록_블록목록과_본문을_읽는다():
    text = (
        "---\n"
        "# 주석\n"
        "name: web\n"
        'description: "웹: 검색"\n'
        "enabled: true\n"
        "off: false\n"
        "caps: [a.b, c]\n"
        "more:\n"
        "  - x\n"
        "  - 'y z'\n"
        "empty: []\n"
        "---\n"
        "본문 첫줄\n"
        "둘째줄\n"
    )
    meta, body = parse_document(text)
    assert meta == {
        "name": "web",
        "description": "웹: 검색",
        "enabled": "true",
        "off": "false",
        "caps": ["a.b", "c"],
        "more": ["x", "y z"],
        "empty": [],
    }
    assert body == "본문 첫줄\n둘째줄\n"


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("본문만\n", "프론트매터"),
        ("---\nname: a\n", "닫는"),
        ("---\nname: a\nname: b\n---\n", "중복"),
        ("---\n이상한 줄\n---\n", "2번째 줄"),
        ("---\nBad Key: 1\n---\n", "키"),
        ("---\n  - 고아항목\n---\n", "목록"),
        ("---\nk: [a, b\n---\n", "목록"),
    ],
)
def test_잘못된_형식은_사유와_함께_거부한다(text, reason):
    with pytest.raises(FrontmatterError, match=reason):
        parse_document(text)


def test_값이_빈_키는_빈_문자열이고_뒤에_목록이_없을_때만이다():
    meta, _ = parse_document("---\nhealth:\nname: a\n---\n")
    assert meta["health"] == ""
