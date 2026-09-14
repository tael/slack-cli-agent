"""기동 전 점검을 전부 돌리고 결과를 모은다.

원본은 `restart.sh` 가 점검 스크립트 여러 개를 순서대로 실행하다 하나가
실패하면 그 자리에서 멈춘다. 사람이 한 번에 여러 문제를 고칠 수 있도록,
여기서는 첫 실패에서 멈추지 않고 점검을 전부 실행한 뒤 모아서 돌려준다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .check import CheckResult, PreflightCheck, PreflightContext


@dataclass(frozen=True)
class PreflightReport:
    """점검 전부를 실행한 결과."""

    results: tuple[CheckResult, ...]

    @property
    def bootable(self) -> bool:
        """fatal 실패가 하나도 없으면 기동 가능하다."""
        return not any((not r.ok) and r.fatal for r in self.results)

    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if not r.ok)

    def fatal_failures(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if not r.ok and r.fatal)


class PreflightRunner:
    """등록된 점검을 전부 실행한다."""

    def __init__(self, checks: Sequence[PreflightCheck]) -> None:
        self._checks = tuple(checks)

    def run_all(self, ctx: PreflightContext) -> PreflightReport:
        results = tuple(check.run(ctx) for check in self._checks)
        return PreflightReport(results=results)
