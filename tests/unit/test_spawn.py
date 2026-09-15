"""TaskSpawner 시험."""

from __future__ import annotations

import logging
import threading

import pytest

from slack_cli_agent.core.spawn import InlineTaskSpawner, ThreadTaskSpawner


def test_inline_은_그_자리에서_돌린다() -> None:
    done: list[str] = []
    InlineTaskSpawner().spawn("점검", lambda: done.append("했다"))
    assert done == ["했다"]


def test_inline_은_예외를_기록하고_호출자에게_안_넘긴다(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """예외가 올라가면 슬랙 이벤트 처리기가 그 뒤 이벤트를 못 받는다."""
    def boom() -> None:
        raise RuntimeError("터졌다")

    with caplog.at_level(logging.ERROR):
        InlineTaskSpawner().spawn("점검", boom)

    assert "터졌다" in caplog.text


def test_thread_는_호출자를_막지_않는다() -> None:
    released = threading.Event()
    finished = threading.Event()

    def work() -> None:
        released.wait(5)
        finished.set()

    ThreadTaskSpawner().spawn("점검", work)
    assert not finished.is_set()
    released.set()
    assert finished.wait(5)


def test_thread_안에서_난_예외를_기록한다(caplog: pytest.LogCaptureFixture) -> None:
    done = threading.Event()

    def boom() -> None:
        try:
            raise RuntimeError("터졌다")
        finally:
            done.set()

    with caplog.at_level(logging.ERROR):
        ThreadTaskSpawner().spawn("점검", boom)
        assert done.wait(5)
        for thread in threading.enumerate():
            if thread.name == "점검":
                thread.join(5)

    assert "터졌다" in caplog.text
