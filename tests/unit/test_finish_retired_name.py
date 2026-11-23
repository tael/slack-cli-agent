"""묻지 않은 폐기 봇 이름을 답에 쓰면 Stop 훅이 막는지 (2026-11-23 private-taeil)."""

from __future__ import annotations

from pathlib import Path

from slack_cli_agent.guard.finish_review import Turn, default_registry, retired_name


def _env(tmp_path: Path) -> dict[str, str]:
    (tmp_path / "retired_names.txt").write_text("# 주석\n데이전트\ndeagent  # 사용자명\n", encoding="utf-8")
    return {"SCA_STATE_DIR": str(tmp_path)}


def test_묻지_않은_폐기_이름을_잡는다(tmp_path: Path) -> None:
    turn = Turn(answer="gh 로그인 정리함.\n\n## 데이전트 상태\n- 잡 0개", user_prompt="gh 인증 바꿔")
    assert retired_name(turn, _env(tmp_path)) == "데이전트"


def test_대소문자_무시(tmp_path: Path) -> None:
    turn = Turn(answer="DEAGENT 계정 삭제 상태", user_prompt="슬랙 앱 정리")
    assert retired_name(turn, _env(tmp_path)) == "deagent"


def test_요청이_그_이름을_물으면_통과(tmp_path: Path) -> None:
    turn = Turn(answer="데이전트는 15:16 삭제됨", user_prompt="데이전트 지금 어때?")
    assert retired_name(turn, _env(tmp_path)) is None


def test_목록이_없으면_통과(tmp_path: Path) -> None:
    turn = Turn(answer="데이전트 상태", user_prompt="x")
    assert retired_name(turn, {}) is None
    assert retired_name(turn, {"SCA_STATE_DIR": str(tmp_path)}) is None


def test_공통_규칙에_등록돼_있다() -> None:
    assert "retired_name" in default_registry.names()
