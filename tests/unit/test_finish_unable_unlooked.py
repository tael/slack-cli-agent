"""도구 호출 0회로 볼 수 없다, 알 수 없다고 답한 턴을 Stop 훅이 막는지 (2026-11-10 sandbox-rnd 19:01)."""

from __future__ import annotations

from slack_cli_agent.guard.finish_review import (
    ToolCall,
    Turn,
    decide,
    default_registry,
    unable_unlooked,
)

#: 2026-11-10 sandbox-rnd 19:01 원문. 이 턴은 도구를 한 번도 부르지 않았다.
ORIGINAL = "프로필 교체 확인함. 새 이미지는 직접 볼 수 없어 평가는 못 함.\n[[NOWATCH]]"

ONE_CALL = (ToolCall(name="Bash", input={"command": "curl -s -o /tmp/p.png https://example"}, result="ok"),)


def test_원문_호출_0회는_잡는다() -> None:
    assert unable_unlooked(Turn(answer=ORIGINAL)) == "볼 수 없"


def test_원문_호출_0회는_decide_가_막는다() -> None:
    decision = decide(Turn(answer=ORIGINAL))
    assert decision.action == "block"
    assert "unable_unlooked" in [f.name for f in decision.findings]


def test_원문_호출_1회는_통과한다() -> None:
    assert unable_unlooked(Turn(answer=ORIGINAL, tool_calls=ONE_CALL)) is None
    decision = decide(Turn(answer=ORIGINAL, tool_calls=ONE_CALL))
    assert "unable_unlooked" not in [f.name for f in decision.findings]


def test_문형이_바뀌어도_잡는다() -> None:
    for answer in (
        "첨부 내용은 알 수 없음.",
        "그 이미지는 보지 못해서 평가는 어려움.",
        "새 프로필은 확인할 수 없었음.",
        "화면은 못 봄.",
        "평가는 못 했음.",
        "본문 확인 불가.",
    ):
        assert unable_unlooked(Turn(answer=answer)), answer


def test_불가_주장이_없으면_통과한다() -> None:
    assert unable_unlooked(Turn(answer="프로필 교체 확인함. 연두색 몸통 캐릭터임.")) is None


def test_인용과_침묵은_통과한다() -> None:
    assert unable_unlooked(Turn(answer='지적 원문 : "볼 수 없어 평가는 못 함" 을 고침.')) is None
    assert unable_unlooked(Turn(answer="[[침묵]]")) is None


def test_공통_규칙에_등록돼_있다() -> None:
    assert "unable_unlooked" in default_registry.names()
