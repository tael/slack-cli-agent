"""core.markers 로 옮긴 ELAPSED_LINE / ELAPSED_MODEL_LINE 회귀 시험.

W4-B: slack 계층이 guard 계층을 더 이상 import 하지 않는지, 정규식이
guard.watch 로 재노출되어도 core.markers 와 같은 객체인지, 정규식 동작
자체가 원본과 같은지를 고정한다.
"""

from __future__ import annotations

import ast
from pathlib import Path

from slack_cli_agent.core.markers import ELAPSED_LINE, ELAPSED_MODEL_LINE

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
