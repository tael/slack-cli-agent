"""UsageCheck 시험.

원본 `bot.py` 의 `usage_watch`(6939) 에 해당한다. 판정과 알림은 운영자가 건
스크립트가 하고, 봇은 주기 실행과 결과 기록만 맡는다.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from slack_cli_agent.core.usage_check import CommandResult, UsageCheck


def _성공(output: str = "잔여 62%") -> CommandResult:
    return CommandResult(returncode=0, stdout=output, stderr="")


def test_명령이_없으면_실행하지_않는다() -> None:
    불린다: list[Sequence[str]] = []

    def run(command: Sequence[str], timeout_sec: float) -> CommandResult:
        불린다.append(command)
        return _성공()

    check = UsageCheck(command=(), run=run)

    assert check.check_once() is False
    assert 불린다 == []


def test_명령을_설정한_제한시간과_함께_부른다() -> None:
    기록: list[tuple[Sequence[str], float]] = []

    def run(command: Sequence[str], timeout_sec: float) -> CommandResult:
        기록.append((command, timeout_sec))
        return _성공()

    check = UsageCheck(command=("usage.py", "--check"), run=run, timeout_sec=30.0)

    assert check.check_once() is True
    assert 기록 == [(("usage.py", "--check"), 30.0)]


def test_성공하면_표준출력을_기록한다(caplog) -> None:  # type: ignore[no-untyped-def]
    check = UsageCheck(command=("usage.py",), run=lambda cmd, timeout: _성공("잔여 62%"))

    with caplog.at_level(logging.INFO):
        check.check_once()

    assert "잔여 62%" in caplog.text


def test_종료코드가_0_이_아니면_경고로_남긴다(caplog) -> None:  # type: ignore[no-untyped-def]
    실패 = CommandResult(returncode=2, stdout="", stderr="한도 파일 없음")
    check = UsageCheck(command=("usage.py",), run=lambda cmd, timeout: 실패)

    with caplog.at_level(logging.INFO):
        assert check.check_once() is True

    assert "한도 파일 없음" in caplog.text
    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_실행이_예외를_내도_주기를_끊지_않는다(caplog) -> None:  # type: ignore[no-untyped-def]
    def 터진다(command: Sequence[str], timeout_sec: float) -> CommandResult:
        raise OSError("실행 파일 없음")

    check = UsageCheck(command=("usage.py",), run=터진다)

    with caplog.at_level(logging.INFO):
        assert check.check_once() is True

    assert "실행 파일 없음" in caplog.text


def test_출력이_길면_잘라_기록한다(caplog) -> None:  # type: ignore[no-untyped-def]
    check = UsageCheck(command=("usage.py",), run=lambda cmd, timeout: _성공("가" * 500))

    with caplog.at_level(logging.INFO):
        check.check_once()

    assert "가" * 201 not in caplog.text
