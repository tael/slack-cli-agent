"""요청 문장과 맥락으로 workflow 를 고르는 규칙 라우터.

순수 함수다. 파이프라인에 연결하지 않았고 고른 결과로 동작을 바꾸지 않는다.
다음 단계(shadow)에서 결과를 trace 태그로만 기록한다.

판정 순서:

1. 명시 접두사 (`!웹`, `!신지`, `!깊게`). 문장 맨 앞(멘션 뒤)에 있을 때만 쓴다.
   슬랙 `/` 는 슬래시 명령과 겹쳐서 쓰지 않는다.
2. 키워드 규칙. RULES 순서대로 먼저 걸린 workflow 가 이긴다.
3. 스레드 후속: 규칙이 안 걸리고 직전 봇 답의 workflow 가 deep 이 아니면 이어받는다.
4. 채널 기본값: 호출자가 준 매핑에 있을 때만 쓴다.
5. 전부 미적중이면 deep, matched=False.

키워드 매칭은 `core.mcp_routing._matches` 를 재사용한다. 패턴은 대소문자를 무시한
부분 문자열이고 `re:` 로 시작하면 정규식이다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from slack_cli_agent.core.mcp_routing import _matches

#: 규칙을 바꿀 때마다 올린다. shadow 기록에서 어느 규칙판의 결과인지 구분한다.
ROUTER_VERSION = "rule-v1"
DEFAULT_WORKFLOW = "deep"

WEB_LOOKUP = "web-lookup"
SHINJI_MAINTENANCE = "shinji-maintenance"
PERSONAL_DEEP = "personal-deep"

#: 명시 접두사 -> workflow
PREFIXES: dict[str, str] = {
    "!웹": WEB_LOOKUP,
    "!신지": SHINJI_MAINTENANCE,
    "!깊게": DEFAULT_WORKFLOW,
}

#: (workflow, 패턴 목록). 앞에 있을수록 우선한다.
#: 신지 수정은 날씨 같은 낱말이 섞여도 이겨야 하므로 맨 앞이고, 본인 기록은
#: 외부 정보보다 앞이다.
RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (SHINJI_MAINTENANCE, (
        "re:(신지|봇|에이전트).{0,12}(설정|지침|말투|페르소나|프롬프트|코드|소스|재기동|재시작|업데이트|고쳐|수정|개선)",
        "re:(어드민|admin)\\s*채널로\\s*설정",
        "re:모델.{0,4}(변경|바꿔|바꾸)",
        "re:(재기동|재시작)\\s*(해|하|시켜)",
        "re:페르소나|persona\\.md|channels\\.json",
        "re:채널(별|마다).{0,8}설정",
        "re:멘션\\s*없이.{0,8}(답|응답|대답)",
    )),
    (PERSONAL_DEEP, (
        "re:내\\s*(개발\\s*)?(성향|이력서?|경력|철학|서사|과거|메일|메모|노트|일정|캘린더|구글\\s*캘린더|개인\\s*캘린더|스타일|습관|기록|드라이브)",
        "re:내가\\s*(예전|과거|지난|전에|이전)",
        "re:네가\\s*아는\\s*내",
        "re:애플\\s*메모|애플\\s*노트|옵시디언|obsidian",
    )),
    (WEB_LOOKUP, (
        "날씨", "기온", "미세먼지", "일기예보",
        "환율", "주가", "시세", "코스피", "코스닥", "나스닥", "비트코인", "이더리움",
        "뉴스",
        "re:(웹|인터넷)\\s*검색|구글링|검색해(서|줘|봐)",
        "re:최신\\s*(버전|소식|릴리스)",
    )),
)

_PREFIX_RE = re.compile(
    r"^(?P<lead>\s*(?:<@[A-Z0-9]+>\s*)*)(?P<prefix>" + "|".join(map(re.escape, PREFIXES)) + r")(?=\s|$)"
)


@dataclass(frozen=True)
class ThreadContext:
    """스레드 맥락. 최소한만 받는다."""

    has_prior_bot_reply: bool = False
    prior_workflow: str = ""


@dataclass(frozen=True)
class RouteDecision:
    workflow_id: str
    router_version: str
    reason: str
    matched: bool


def _decision(workflow: str, reason: str, matched: bool) -> RouteDecision:
    return RouteDecision(workflow, ROUTER_VERSION, reason, matched)


def strip_prefix(text: str) -> str:
    """명시 접두사를 걷어 낸다. 앞의 멘션은 남긴다. 접두사가 없으면 그대로다."""
    m = _PREFIX_RE.match(text)
    if not m:
        return text
    rest = text[m.end():].lstrip()
    return (m.group("lead").strip() + " " + rest).strip() if m.group("lead").strip() else rest


def route(
    text: str,
    channel_id: str = "",
    thread: ThreadContext | None = None,
    channel_defaults: Mapping[str, str] | None = None,
) -> RouteDecision:
    m = _PREFIX_RE.match(text)
    if m:
        prefix = m.group("prefix")
        return _decision(PREFIXES[prefix], f"prefix:{prefix}", True)
    if text.strip():
        for workflow, patterns in RULES:
            for pattern in patterns:
                if _matches(pattern, text):
                    return _decision(workflow, f"rule:{workflow}:{pattern}", True)
    if (
        thread is not None
        and thread.has_prior_bot_reply
        and thread.prior_workflow
        and thread.prior_workflow != DEFAULT_WORKFLOW
    ):
        return _decision(thread.prior_workflow, f"thread-inherit:{thread.prior_workflow}", True)
    if channel_defaults and channel_id in channel_defaults:
        return _decision(channel_defaults[channel_id], f"channel-default:{channel_id}", True)
    return _decision(DEFAULT_WORKFLOW, "no-rule", False)
