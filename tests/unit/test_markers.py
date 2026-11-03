"""core.markers 로 옮긴 ELAPSED_LINE / ELAPSED_MODEL_LINE 회귀 시험.

W4-B: slack 계층이 guard 계층을 더 이상 import 하지 않는지, 정규식이
guard.watch 로 재노출되어도 core.markers 와 같은 객체인지, 정규식 동작
자체가 원본과 같은지를 고정한다.
"""

from __future__ import annotations

import ast
from pathlib import Path

from slack_cli_agent.core.markers import (
    ASK_TAG,
    ELAPSED_LINE,
    ELAPSED_MODEL_LINE,
    NOWATCH_TAG,
    take_ask_tag,
    take_nowatch_tag,
)

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "slack_cli_agent"


def _imports_guard(path: Path) -> bool:
    """그 파일의 import 문 중 guard 패키지를 가져오는 것이 있는지 본다."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "slack_cli_agent.guard" or module.startswith(
                "slack_cli_agent.guard."
            ):
                return True
            # 상대 import (from ..guard.watch import ...) 형태도 본다.
            if node.level and node.module and "guard" in node.module.split("."):
                return True
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "slack_cli_agent.guard" or alias.name.startswith(
                    "slack_cli_agent.guard."
                ):
                    return True
    return False


def test_gate_does_not_import_guard() -> None:
    path = SRC_ROOT / "slack" / "gate.py"
    assert not _imports_guard(path), "slack/gate.py 가 guard 패키지를 import 한다"


def test_publisher_does_not_import_guard() -> None:
    path = SRC_ROOT / "slack" / "publisher.py"
    assert not _imports_guard(path), "slack/publisher.py 가 guard 패키지를 import 한다"


def test_elapsed_line_strips_single_line() -> None:
    body = "본문입니다.\n> 걸린 시간 : 12초"
    assert ELAPSED_LINE.sub("", body) == "본문입니다."


def test_elapsed_line_strips_multiple_lines() -> None:
    body = "본문입니다.\n> 걸린 시간 : 12초\n> 걸린 시간 : 5초\n"
    assert ELAPSED_LINE.sub("", body) == "본문입니다."


def test_elapsed_model_line_strips_model_and_engine_labels() -> None:
    body_model = "본문입니다.\n> 실행 모델 : sonnet"
    body_engine = "본문입니다.\n> 실행 엔진 : sonnet"
    assert ELAPSED_MODEL_LINE.sub("", body_model) == "본문입니다."
    assert ELAPSED_MODEL_LINE.sub("", body_engine) == "본문입니다."


def test_core_markers_and_guard_watch_are_the_same_object() -> None:
    from slack_cli_agent.guard import watch

    assert watch.ELAPSED_LINE is ELAPSED_LINE
    assert watch.ELAPSED_MODEL_LINE is ELAPSED_MODEL_LINE


class TestAskTag:
    """[[ASK]] 표식 — bot.py:1507,8273-8278 과 같아야 한다."""

    def test_tag_문자열이_같다(self) -> None:
        assert ASK_TAG == "[[ASK]]"

    def test_표식만_있는_줄을_뗀다(self) -> None:
        text, had = take_ask_tag("진행할지 말씀 주시면 그때 실행하겠습니다.\n[[ASK]]")
        assert had is True
        assert text == "진행할지 말씀 주시면 그때 실행하겠습니다."

    def test_표식_뒤_빈_줄까지_먹지_않는다(self) -> None:
        """2026-11-01 : 표식 줄 다음의 WATCH/NOWATCH 가 앞 문장에 들러붙는
        사고가 있었다 — 뒤따르는 빈 줄은 건드리지 않는다."""
        text, had = take_ask_tag("답변입니다.\n[[ASK]]\n\n[[NOWATCH]]")
        assert had is True
        assert text == "답변입니다.\n\n[[NOWATCH]]"

    def test_표식이_없으면_그대로다(self) -> None:
        text, had = take_ask_tag("그냥 답변입니다")
        assert had is False
        assert text == "그냥 답변입니다"

    def test_문장_중간의_언급은_표식으로_안_본다(self) -> None:
        """줄 전체가 표식만이어야 한다. 규약을 설명하는 문장은 지워지면
        안 된다 (2026-11-01 사고: '제가를 붙인' 처럼 글자가 깨졌다)."""
        text, had = take_ask_tag("[[ASK]] 표식은 답 대기를 뜻합니다.")
        assert had is False
        assert text == "[[ASK]] 표식은 답 대기를 뜻합니다."

    def test_빈_문자열은_그대로다(self) -> None:
        text, had = take_ask_tag("")
        assert had is False
        assert text == ""


class TestNowatchTag:
    """[[NOWATCH]] 표식 — bot.py:1538-1544 와 같아야 한다. 모델이 규약대로
    붙이는 '감시 필요 없음' 신호이고, 사용자 화면에 나가면 안 된다."""

    def test_tag_문자열이_같다(self) -> None:
        assert NOWATCH_TAG == "[[NOWATCH]]"

    def test_끝에_붙은_표식을_뗀다(self) -> None:
        text, had = take_nowatch_tag("정리해 드렸습니다.\n[[NOWATCH]]")
        assert had is True
        assert text == "정리해 드렸습니다."

    def test_빈_줄을_사이에_두고_붙어도_뗀다(self) -> None:
        """리치 렌더(표)에서 본문 뒤 빈 줄 다음에 표식만 남아 fallback
        text 로 새어 나간 사고를 막는다."""
        text, had = take_nowatch_tag("| 품목 | 값 |\n| --- | --- |\n\n[[NOWATCH]]")
        assert had is True
        assert text == "| 품목 | 값 |\n| --- | --- |"

    def test_표식이_없으면_그대로다(self) -> None:
        text, had = take_nowatch_tag("그냥 답변입니다")
        assert had is False
        assert text == "그냥 답변입니다"

    def test_문장_중간의_언급은_표식으로_안_본다(self) -> None:
        """줄 전체가 표식만이어야 한다 — 규약을 설명하는 문장은 지우지 않는다."""
        text, had = take_nowatch_tag("[[NOWATCH]] 는 감시가 필요 없다는 뜻입니다.")
        assert had is False
        assert text == "[[NOWATCH]] 는 감시가 필요 없다는 뜻입니다."

    def test_빈_문자열은_그대로다(self) -> None:
        text, had = take_nowatch_tag("")
        assert had is False
        assert text == ""
