"""불릿 형식 5why 사슬 검수 (2026-11-13 sandbox-rnd 14:48 "서버 부하 관점으로 5why")."""

from __future__ import annotations

from slack_cli_agent.guard.finish_review import ToolCall, Turn, causal_chain, decide

#: 14:48 게시 답 요지. 슬랙 보고만 읽고 원천 조회 0회였다.
ORIGINAL = """prime-baemin 이 공지 121688 의 동시 조회 버스트를 받아 응답이 4-7초로 늘어난 장애임.

## 5 Why

- 왜 POS 응답이 지연되고 일부 기능이 실패했나
  - baemin 이 초당 700-970건을 받아 응답 중앙값이 4-7초가 됨
- 왜 baemin 에서만 응답이 느려졌나
  - 같은 구간에 api 는 초당 754건 피크에서 응답 0.62초임
- 왜 요청이 한 순간에 몰렸나
  - 주영익님은 대상이 대량, 전체 상점일 가능성이 크다고 봄
- 반증 확인
  - DB, Redis 가 여유이고 api 가 같은 사양에서 정상이라 공통 인프라 한도 가설은 깨짐

## 검증할 것

- baemin 파드 20개 재시작의 원인 : liveness 기준 3초 5회와의 관계를 k8s 이벤트로 확인 필요
- 관리 화면이 apply_all 과 target_stores 를 함께 보낸 경위 : 서비스개발1팀 확인 대상
[[NOWATCH]]"""

SLACK_ONLY = (
    ToolCall(name="ToolSearch", result="ok"),
    ToolCall(name="mcp__slack__slack_thread_replies", result="초당 700 970 754 0.62 4,417 72,001"),
)

GOOD = """baemin 워커 포화로 liveness 실패 후 재시작된 장애임.

- 왜 응답이 느려졌나
  - 요청 처리 p50 4,812ms, 같은 시각 api 612ms
- 왜 baemin 만 포화됐나
  - 공지 조회 1건당 UAA 호출 3회, 평균 1,450ms
- 반증 확인
  - 공지 조회를 뺀 구간 p50 640ms 로 정상
[[NOWATCH]]"""

PRIMARY = (
    ToolCall(name="Bash", input={"command": "kibana.sh ..."}, result="p50 4812 612 1450 640"),
)


def test_원문은_막힌다() -> None:
    hit = causal_chain(Turn(answer=ORIGINAL, tool_calls=SLACK_ONLY))
    assert hit is not None
    assert "원천 조회 0회" in hit
    assert "가능성이 크" in hit
    assert "반증 줄에 도구 결과 수치가 없음" in hit
    assert "k8s 이벤트로 확" in hit
    assert "서비스개발1팀" not in hit


def test_원문은_decide_가_막는다() -> None:
    decision = decide(Turn(answer=ORIGINAL, tool_calls=SLACK_ONLY))
    assert decision.action == "block"
    assert "causal_chain" in [f.name for f in decision.findings]


def test_원천_수치로_뒷받침된_사슬은_통과한다() -> None:
    assert causal_chain(Turn(answer=GOOD, tool_calls=PRIMARY)) is None


def test_사슬이_아닌_답은_검사하지_않는다() -> None:
    assert causal_chain(Turn(answer="적재 정상 재개됨.\n[[NOWATCH]]")) is None
