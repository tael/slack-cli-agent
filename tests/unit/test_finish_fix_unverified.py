"""해결·재발방지를 주장하면서 고친 뒤 원래 실패를 재현하지 않으면 막는지 (2026-11-23 private-taeil)."""

from __future__ import annotations

from slack_cli_agent.guard.finish_review import ToolCall, Turn, default_registry, fix_unverified

REQ = (
    "[15:36:56 오너]\n- ssh 접속: MB-peteam.local 과 10.5.201.183 에 peagent 계정으로 접속했지만 "
    "Permission denied\n접속불가문제 해결."
)


def _edit(path: str) -> ToolCall:
    return ToolCall(name="Edit", input={"file_path": path})


def _bash(cmd: str) -> ToolCall:
    return ToolCall(name="Bash", input={"command": cmd}, result="ok")


def test_지침만_고치고_재발방지라_쓰면_막는다() -> None:
    turn = Turn(answer="접속 문제 해결함.\n- 지식에 규칙 추가", user_prompt=REQ,
                tool_calls=(_bash("ssh -F cfg peteam true"), _edit("/x/persona/knowledge/private-taeil.md")))
    assert (fix_unverified(turn) or "").startswith("doc_only")


def test_고친_뒤_재시험이_없으면_막는다() -> None:
    turn = Turn(answer="재발방지 완료함.", user_prompt=REQ,
                tool_calls=(_edit("/x/ssh/config"),))
    assert (fix_unverified(turn) or "").startswith("no_recheck")


def test_실패한_대상을_재현하지_않으면_막는다() -> None:
    turn = Turn(answer="해결함. 별칭으로 접속됨", user_prompt=REQ,
                tool_calls=(_edit("/x/ssh/config"), _bash("ssh -F /x/ssh/config peteam true")))
    hit = fix_unverified(turn) or ""
    assert hit.startswith("target_miss") and "10.5.201.183" in hit and "MB-peteam.local" in hit


def test_원래_형태로_재현했으면_통과() -> None:
    turn = Turn(answer="해결함. 두 주소 모두 접속됨", user_prompt=REQ,
                tool_calls=(_edit("/x/ssh/config"),
                            _bash("ssh peagent@MB-peteam.local true; ssh peagent@10.5.201.183 true")))
    assert fix_unverified(turn) is None


def test_고친_것이_없으면_대상_아님() -> None:
    turn = Turn(answer="이미 해결됨.", user_prompt=REQ, tool_calls=(_bash("ssh peteam true"),))
    assert fix_unverified(turn) is None


def test_부정문은_주장이_아니다() -> None:
    turn = Turn(answer="15:39 조치로는 재발이 막혀 있지 않았음.", user_prompt=REQ,
                tool_calls=(_edit("/x/persona/knowledge/a.md"),))
    assert fix_unverified(turn) is None


def test_커밋과_적용예약은_수정으로_치지_않는다() -> None:
    turn = Turn(answer="재발방지 완료함.", user_prompt="고쳐",
                tool_calls=(_edit("/x/src/a.py"), _bash("uv run pytest -q"),
                            _bash("git commit -m x && git push"), _bash("printf x > ~/.guchipachi-sca/apply-request")))
    assert fix_unverified(turn) is None


def test_공통_규칙에_등록돼_있다() -> None:
    assert "fix_unverified" in default_registry.names()
