"""Runs every preflight check without stopping at the first failure, so a
person can fix several problems in one pass."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .check import CheckResult, PreflightCheck, PreflightContext


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
        results = tuple(check.run(ctx) for check in self._checks)
        return PreflightReport(results=results)
