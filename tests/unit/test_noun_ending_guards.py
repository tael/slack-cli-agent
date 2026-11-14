"""2026-11-10 : 구치파치 말투를 명사형 요약체로 바꾸면서 판정 정규식이 명사형 문장도 잡는지 확인한다."""
import pytest

from slack_cli_agent.guard import finish_review as f
from slack_cli_agent.guard import watch as w
from slack_cli_agent.slack import gate as g

CASES = [
    (w.PROMISE_WITHOUT_WATCH_RE, "집계 결과 이 스레드에 공유 예정", True),
    (w.PROMISE_WITHOUT_WATCH_RE, "이어서 대조 작업 진행 예정", True),
    (w.PROMISE_WITHOUT_WATCH_RE, "구 서버 종료 예정 11/11", False),
    (f._OFFER_RE, "같은 집계를 돌려 볼 수 있음", True),
    (f._OFFER_RE, "적재 정상 재개됨", False),
    (f._NOT_OPENED_PAST_RE, "본문 열지 않음", True),
    (f._READY_RE, "S3 로그 수집됨", True),
    (f._ABSENCE_RE, "셔터 로그 없음", True),
    (f._ABSENCE_RE, "영향 없음", False),
    (f._UNCONFIRMED_RE, "원인 확인 불가", True),
    (f._GAVE_UP_RE, "스레드 조회 불가", True),
    (f._STOPPED_RE, "쓰기 권한 없음", True),
    (f._STOPPED_RE, "권한 없이 조회됨", False),
    (g.ASKED_BACK, "**옵션 2로 진행할까요?**", True),
    (g.ASKED_BACK, "재기동 완료함", False),
]


@pytest.mark.parametrize(("pattern", "text", "expected"), CASES)
def test_noun_ending(pattern, text, expected):
    assert bool(pattern.search(text)) is expected
