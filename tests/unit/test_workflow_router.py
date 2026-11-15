"""규칙 라우터 (shadow 단계) 시험. 파이프라인에는 아직 연결되지 않는다."""

from __future__ import annotations

import pytest

from slack_cli_agent.routing.workflow_router import (
    DEFAULT_WORKFLOW,
    ROUTER_VERSION,
    RouteDecision,
    ThreadContext,
    route,
    strip_prefix,
)


def test_미적중은_deep_기본값이고_matched_false():
    d = route("이 함수가 왜 느린지 설명해 줘")
    assert d == RouteDecision(DEFAULT_WORKFLOW, ROUTER_VERSION, d.reason, False)
    assert d.workflow_id == "deep"
    assert d.reason == "no-rule"


@pytest.mark.parametrize("text", [
    "오늘 서울 날씨 알려줘",
    "지금 달러 환율이 얼마야",
    "오늘 주요 뉴스 알려줘",
    "웹 검색으로 이 라이브러리 최신 버전 찾아줘",
])
def test_외부_실시간_정보는_web_lookup(text):
    d = route(text)
    assert d.workflow_id == "web-lookup"
    assert d.matched is True
    assert d.reason.startswith("rule:web-lookup:")


@pytest.mark.parametrize("text", [
    "신지 말투 지침 고쳐줘",
    "이 채널을 어드민 채널로 설정해",
    "소넷 모델로 변경해",
    "신지 재기동해",
    "페르소나 파일에 한 줄 추가해",
])
def test_신지_자신의_설정_수정은_shinji_maintenance(text):
    d = route(text)
    assert d.workflow_id == "shinji-maintenance"
    assert d.reason.startswith("rule:shinji-maintenance:")


@pytest.mark.parametrize("text", [
    "네가아는 내 이력서 10줄 요약",
    "내 과거 메일에서 스톡옵션 관련 자료가 있었니",
    "내 개발 성향을 정리해 줘",
    "애플 메모에서 내 철학을 더 뽑아내봐",
])
def test_본인_기록을_모아야_하는_질문은_personal_deep(text):
    d = route(text)
    assert d.workflow_id == "personal-deep"
    assert d.reason.startswith("rule:personal-deep:")


def test_내일_같은_낱말은_본인_규칙에_걸리지_않는다():
    assert route("내일 서울 날씨").workflow_id == "web-lookup"


def test_신지_수정이_날씨_낱말보다_우선한다():
    assert route("신지 날씨 기능 코드 고쳐줘").workflow_id == "shinji-maintenance"


@pytest.mark.parametrize("prefix,expected", [
    ("!웹", "web-lookup"),
    ("!신지", "shinji-maintenance"),
    ("!깊게", "deep"),
])
def test_명시_접두사는_규칙보다_우선한다(prefix, expected):
    # 본문이 다른 규칙에 걸리더라도 접두사가 이긴다
    d = route(f"{prefix} 신지 말투 지침 고쳐줘 오늘 날씨")
    assert d.workflow_id == expected
    assert d.matched is True
    assert d.reason == f"prefix:{prefix}"


def test_멘션_뒤의_접두사도_인식한다():
    assert route("<@U0C1WV8DRU1> !웹 코스피").workflow_id == "web-lookup"


def test_문장_중간의_접두사는_무시한다():
    assert route("이건 !웹 으로 보내지 마").reason != "prefix:!웹"


def test_strip_prefix는_접두사와_앞뒤_공백을_뺀다():
    assert strip_prefix("!웹  오늘 날씨") == "오늘 날씨"
    assert strip_prefix("<@U1> !깊게 이거") == "<@U1> 이거"
    assert strip_prefix("접두사 없음") == "접두사 없음"


def test_스레드_후속은_규칙_미적중일_때_직전_workflow를_잇는다():
    ctx = ThreadContext(has_prior_bot_reply=True, prior_workflow="web-lookup")
    d = route("아니 부산으로", thread=ctx)
    assert d.workflow_id == "web-lookup"
    assert d.matched is True
    assert d.reason == "thread-inherit:web-lookup"


def test_스레드_후속도_규칙이_적중하면_규칙이_이긴다():
    ctx = ThreadContext(has_prior_bot_reply=True, prior_workflow="web-lookup")
    assert route("신지 말투 지침 고쳐줘", thread=ctx).workflow_id == "shinji-maintenance"


def test_직전_workflow가_deep이면_잇지_않고_미적중이다():
    ctx = ThreadContext(has_prior_bot_reply=True, prior_workflow="deep")
    d = route("그래서?", thread=ctx)
    assert (d.workflow_id, d.matched, d.reason) == ("deep", False, "no-rule")


def test_채널_기본값은_주입한_매핑만_쓴다():
    assert route("그냥 인사", channel_id="C1").matched is False
    d = route("그냥 인사", channel_id="C1", channel_defaults={"C1": "web-lookup"})
    assert (d.workflow_id, d.matched, d.reason) == ("web-lookup", True, "channel-default:C1")


def test_빈_문장은_미적중():
    d = route("   ")
    assert (d.workflow_id, d.matched) == ("deep", False)
