"""Slack 이벤트 텍스트를 다시 게시할 수 있는 형태로 되돌린다.

두 자리가 이것을 쓴다 - 검수 원장의 답변 대조와 느린 요청 보고의 요청 행이다.
자리마다 따로 두면 한쪽만 고쳐 같은 원문이 보고에 따라 다르게 나온다(sca-3o1).
"""

from __future__ import annotations

import html
import re

_LINK_LABEL = re.compile(r"<([^<>|]+)\|([^<>]+)>")
_BRACKET = re.compile(r"<([^<>]+)>")


def clean_excerpt(text: str | None) -> str | None:
    # Slack's event API HTML-escapes text and already-rendered links come
    # as `<url|label>`; re-pasting that verbatim would double-wrap in
    # angle brackets. Unescape entities, keep only the link label, and
    # strip remaining brackets until nothing changes.
    if not text:
        return text
    out = html.unescape(text)
    out = _LINK_LABEL.sub(r"\2", out)
    prev = None
    while prev != out:
        prev = out
        out = _BRACKET.sub(r"\1", out)
    return out
