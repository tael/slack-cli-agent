"""Preflight check contract: each check is a class, and the shared run loop
lives in `PreflightRunner`."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from ..config.profile import Profile


@dataclass(frozen=True)
class PreflightContext:
    profile: Profile


@dataclass(frozen=True)
class CheckResult:
    """fatal=False downgrades a failing check to a warning that doesn't block boot; default is True."""

    ok: bool
    detail: str
    fatal: bool = True


class PreflightCheck(ABC):
    name: ClassVar[str]

    @abstractmethod
    def run(self, ctx: PreflightContext) -> CheckResult:
        """Runs the check. Returns a result rather than raising."""
