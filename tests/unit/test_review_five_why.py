"""review/five_why.py 의 5 Why 사슬 점검 시험 (B8).

판정 규칙은 마메치 bot.py check_five_why 를 옮겼다. 다른 점은 `## 5 Why`
절이 없는 상세를 통과시키는 것 하나다. 기본 부검 지침에는 그 절이 없다.
"""

from __future__ import annotations

from slack_cli_agent.review.five_why import broken_five_why_prompt, check_five_why

이어진_표 = """## 5 Why
| 단계 | 물음 | 답 | 근거 |
|---|---|---|---|
| Why 1 | 왜 답변이 틀린 테이블을 안내했나 | 답변이 스키마 캐시 대신 기억으로 테이블을 골랐다 | 대화록 14:02 발화 |
| Why 2 | 왜 스키마 캐시 대신 기억으로 골랐나 | 지침에 캐시 조회 순서가 정해져 있지 않았다 | _도구지침.md 조회 절 |
| Why 3 | 왜 지침에 캐시 조회 순서가 없었나 | 프롬프트 조립 코드가 도구지침 절을 넣지 않았다 | sections.py build_tool_note |
| Why 4 | 왜 조립 코드가 그 절을 넣지 않았나 | build_tool_note 가 채널 모드 private 에서만 실행된다 | sections.py build_tool_note |
| Why 5 | 왜 private 모드에서만 실행되나 | mode_gate 조건이 이관 때 좁혀졌다 | settings.py mode_gate |

## 근본원인
settings.py 의 mode_gate 조건이 이관 때 좁혀져 다른 모드에서 도구지침이 빠진다.
"""

끊긴_표 = """## 5 Why
| 단계 | 물음 | 답 | 근거 |
|---|---|---|---|
| Why 1 | 왜 답변이 틀렸나 | 답변이 틀렸다 | 대화록 |
| Why 2 | 왜 날씨가 흐렸나 | 구름이 많았다 | 미확인 |
| Why 3 | 왜 구름이 많았나 | 습도가 높았다 | 미확인 |

## 근본원인
알 수 없다.
"""


def test_이어진_사슬은_통과한다() -> None:
    assert check_five_why(이어진_표) == []


def test_5_Why_절이_없으면_점검하지_않는다() -> None:
    """기본 postmortem_note.md 형식에는 5 Why 절이 없다. 매번 재작성을 부르면 안 된다."""
    assert check_five_why("## 원인\n| 원인 | 근거 위치 |\n|---|---|\n| a | b |\n") == []


def test_끊긴_사슬을_항목별로_짚는다() -> None:
    bad = check_five_why(끊긴_표)
    assert any("다섯 단계" in b for b in bad)
    assert any("Why 1 의 답과 Why 2 의 물음이 이어지지 않는다" in b for b in bad)
    assert any("Why 1 의 답이 물음을 되풀이한다" in b for b in bad)


def test_빈_근거와_빈_답을_짚는다() -> None:
    표 = 이어진_표.replace("| sections.py build_tool_note |\n| Why 4", "|  |\n| Why 4", 1)
    bad = check_five_why(표)
    assert "Why 3 의 근거 칸이 비어 있다. 확인하지 못했으면 미확인이라 쓴다." in bad


def test_근본원인이_Why5_를_정리하지_않으면_짚는다() -> None:
    표 = 이어진_표.split("## 근본원인")[0] + "## 근본원인\n답변이 기억으로 테이블을 골랐다.\n"
    bad = check_five_why(표)
    assert any("근본원인이 Why 5 를 정리하지 않는다" in b for b in bad)


def test_근본원인_절이_없으면_짚는다() -> None:
    bad = check_five_why(이어진_표.split("## 근본원인")[0])
    assert bad == ["`## 근본원인` 절이 없다."]


def test_재작성_요청은_항목과_구분선을_담는다() -> None:
    prompt = broken_five_why_prompt(["Why 2 의 근거 칸이 비어 있다."], "===상세===")
    assert "- Why 2 의 근거 칸이 비어 있다." in prompt
    assert "`===상세===` 구분선도 그대로 둔다" in prompt
