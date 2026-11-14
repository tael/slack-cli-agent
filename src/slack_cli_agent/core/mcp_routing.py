"""요청 문장으로 이 턴에 붙일 MCP 서버 묶음을 고른다 (S2 컨텍스트 다이어트 2단계).

MCP 서버 하나를 붙이면 그 서버의 도구 정의 전체가 매 호출 컨텍스트에
실린다. 2026-11-08 측정에서 구치파치 첫 호출 268.5k 토큰 중 약 167k 가 도구
정의였고, notion 서버 하나가 그중 65% 였다. 소유자 턴은 허용목록이 와일드카드라
1단계(허용목록 기준 필터)로는 줄지 않으므로 여기서 요청 종류로 고른다.

프로필 settings.mcp_routing 형식:

    {
      "always": ["slack", "local-rag"],
      "groups": {
        "data": {"when": ["스노우플레이크", "re:\\bsql\\b"], "servers": ["snowflake"]}
      },
      "fallback": "all"
    }

- when 항목은 대소문자 무시 부분 문자열이다. "re:" 로 시작하면 정규식이다.
- 어느 묶음에도 안 맞으면(분류가 불확실하면) fallback 을 따른다. "all" (기본)
  은 전부 붙이고, "always" 는 always 묶음만 붙인다.
- 재개 턴은 그 세션이 앞서 붙인 서버를 계속 붙인다. 이전 턴이 전부를 붙였으면
  이번에도 전부다. 대화 기록 속 도구 호출이 가리키는 서버가 사라지지 않게 한다.
- 설정이 없거나 형식이 틀리면 None (지금처럼 전부 붙임) 이다.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

#: SessionRecord.mcp_servers 에서 "전부 붙였다" 는 표시.
ALL_SERVERS = "*"
REGEX_PREFIX = "re:"


def _names(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable):
        return [str(v) for v in value if str(v)]
    return []


def _matches(pattern: str, text: str) -> bool:
    if pattern.startswith(REGEX_PREFIX):
        try:
            return re.search(pattern[len(REGEX_PREFIX):], text, re.IGNORECASE) is not None
        except re.error:
            logger.warning("mcp_routing 정규식이 잘못됐다: %r", pattern)
            return False
    return pattern.casefold() in text.casefold()


def decode_servers(stored: str) -> frozenset[str] | None:
    """세션에 남긴 값을 읽는다. None 은 전부, 빈 집합은 기록 없음."""
    if stored.strip() == ALL_SERVERS:
        return None
    return frozenset(s.strip() for s in stored.split(",") if s.strip())


def encode_servers(selection: frozenset[str] | None) -> str:
    return ALL_SERVERS if selection is None else ",".join(sorted(selection))


def select_mcp_servers(
    routing: Mapping[str, Any] | None, text: str, previous: str = "",
) -> frozenset[str] | None:
    """이 턴에 붙일 서버 이름. None 은 고르지 않음(전부)이다."""
    if not isinstance(routing, Mapping) or not routing:
        return None
    groups = routing.get("groups")
    if not isinstance(groups, Mapping):
        return None
    earlier = decode_servers(previous)
    if earlier is None:
        return None
    always = set(_names(routing.get("always", ())))
    matched = False
    chosen: set[str] = set()
    for group in groups.values():
        if not isinstance(group, Mapping):
            continue
        if any(_matches(p, text) for p in _names(group.get("when", ()))):
            matched = True
            chosen.update(_names(group.get("servers", ())))
    if not matched:
        fallback = str(routing.get("fallback", "all"))
        if fallback != "always":
            return None
    return frozenset(always | chosen | set(earlier))
