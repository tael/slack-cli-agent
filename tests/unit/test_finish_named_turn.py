"""턴의 주체를 사람 이름으로 특정한 답을 Stop 훅이 막는지 (2026-11-09 private-taeil)."""

from __future__ import annotations

from slack_cli_agent.guard.finish_review import Turn, default_registry, named_turn


def _turn(answer: str) -> Turn:
    return Turn(answer=answer)


def test_이름님_턴을_잡는다() -> None:
    assert named_turn(_turn("그 턴은 김태일님 턴이었고 권한도 전권이었습니다.")) == "김태일님 턴"


def test_붙여쓴_이름님턴도_잡는다() -> None:
    assert named_turn(_turn("이종선님턴에는 Bash 가 없음")) == "이종선님턴"


def test_오너_턴은_통과한다() -> None:
    assert named_turn(_turn("그 턴은 오너 턴이었고 요청자 턴과 다릅니다.")) is None


def test_따옴표_안_인용은_통과한다() -> None:
    assert named_turn(_turn('지적 원문 : "김태일님 턴" 이 아니고 오너 턴')) is None


def test_공통_규칙에_등록돼_있다() -> None:
    assert "named_turn" in default_registry.names()
