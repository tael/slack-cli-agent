"""엔진 자식 프로세스의 stdin.

엔진 CLI(codex exec, claude -p)는 프롬프트를 인자로 받아도 stdin 을 먼저 읽는다.
부모의 열린 stdin 을 물려받으면 EOF 를 못 받아 타임아웃까지 멈춘다
(2026-09-28 codex exec 43분 공전, H67). 부모가 무엇을 물려받았든 같게 동작하도록
엔진 호출은 stdin 을 /dev/null 로 둔다.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from slack_cli_agent.engine.runner import EngineRunner
from slack_cli_agent.engine.stream import run_streaming


@pytest.fixture
def 열린_stdin() -> Iterator[None]:
    """fd 0 을 아무도 쓰지 않는 열린 파이프로 바꾼다. 상속되면 cat 이 멈춘다."""
    r, w = os.pipe()
    saved = os.dup(0)
    os.dup2(r, 0)
    try:
        yield
    finally:
        os.dup2(saved, 0)
        for fd in (saved, r, w):
            os.close(fd)


@pytest.mark.usefixtures("열린_stdin")
def test_스트리밍_경로는_stdin_을_상속하지_않는다(tmp_path) -> None:
    done = run_streaming(["cat"], str(tmp_path), 5, None, lambda _line: None)
    assert done.returncode == 0
    assert done.stdout == ""


@pytest.mark.usefixtures("열린_stdin")
def test_기본_경로는_stdin_을_상속하지_않는다(tmp_path) -> None:
    done = EngineRunner._default_runner(["cat"], str(tmp_path), 5)
    assert done.returncode == 0
    assert done.stdout == ""
