"""Runs every preflight check without stopping at the first failure, so a
person can fix several problems in one pass."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from .check import CheckResult, PreflightCheck, PreflightContext

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreflightReport:
    results: tuple[CheckResult, ...]

    @property
    def bootable(self) -> bool:
        return not any((not r.ok) and r.fatal for r in self.results)

    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if not r.ok)

    def fatal_failures(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if not r.ok and r.fatal)


class PreflightRunner:
    def __init__(self, checks: Sequence[PreflightCheck]) -> None:
        self._checks = tuple(checks)

    def run_all(self, ctx: PreflightContext) -> PreflightReport:
        return PreflightReport(results=tuple(self._run_one(c, ctx) for c in self._checks))

    @staticmethod
    def _run_one(check: PreflightCheck, ctx: PreflightContext) -> CheckResult:
        """A check that raises used to take the whole run with it, losing the
        other checks' results — the opposite of what this class is for. An
        unexpected failure is itself a reason not to boot, so it becomes a
        fatal result (sca-ylz)."""
        try:
            return check.run(ctx)
        except Exception as exc:
            # Type included: a bare str(exc) is empty for several builtins, and
            # the operator then sees a blank reason (코덱스 리뷰).
            log.exception("점검 %s 가 예상 밖 예외로 끝났다", check.name)
            return CheckResult(ok=False, detail=f"점검이 예상 밖 예외로 끝났다 : {exc!r}")
