"""Runs an operator-supplied usage check command on a schedule.

The judgement and any alert belong to that command, not here (bot.py:6939):
a person can run the same script by hand to see the current state, and the
rules can change without restarting the bot. Usage limits also differ per
engine, so a limit rule built into the installed package would only fit one.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

log = logging.getLogger(__name__)

_STDOUT_LIMIT = 200
_STDERR_LIMIT = 300


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def run_command(command: Sequence[str], timeout_sec: float) -> CommandResult:
    done = subprocess.run(
        list(command), capture_output=True, text=True, timeout=timeout_sec, check=False
    )
    return CommandResult(
        returncode=done.returncode, stdout=done.stdout or "", stderr=done.stderr or ""
    )


class UsageCheck:
    def __init__(
        self,
        command: Sequence[str],
        run: Callable[[Sequence[str], float], CommandResult] | None = None,
        *,
        timeout_sec: float = 60.0,
    ) -> None:
        self._command = tuple(command)
        self._run = run or (lambda cmd, timeout: run_command(cmd, timeout))
        self._timeout_sec = timeout_sec

    def check_once(self) -> bool:
        """Returns whether the command was attempted, not whether it succeeded."""
        if not self._command:
            return False
        try:
            result = self._run(self._command, self._timeout_sec)
        except Exception as exc:  # noqa: BLE001 - a failed run must not end the periodic loop
            log.warning("사용량 확인 중 오류 : %s", exc)
            return True
        if result.returncode == 0:
            log.info("사용량 확인 : %s", result.stdout.strip()[:_STDOUT_LIMIT])
        else:
            log.warning("사용량 확인 실패 : %s", result.stderr.strip()[:_STDERR_LIMIT])
        return True
