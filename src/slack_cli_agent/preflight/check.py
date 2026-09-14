"""기동 전 점검의 계약.

원본은 `check.py` 가 절차형 스크립트다. 점검마다 클래스로 나누고
공통 실행은 `PreflightRunner` 에 둔다(01-source-analysis.md 20절,
03-TRD.md 8절).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from ..config.profile import Profile


@dataclass(frozen=True)
class PreflightContext:
    """점검 하나가 보는 입력. 프로필 하나면 모든 점검이 충분하다."""

    profile: Profile


@dataclass(frozen=True)
class CheckResult:
    """점검 하나의 결과.

    fatal=False 면 문제가 있어도 경고만 내고 기동한다. 기본은 True다 —
    원본이 점검 넷을 전부 기동 차단으로 다뤘던 것과 같다.
    """

    ok: bool
    detail: str
    fatal: bool = True


class PreflightCheck(ABC):
    """점검 하나의 계약."""

    name: ClassVar[str]

    @abstractmethod
    def run(self, ctx: PreflightContext) -> CheckResult:
        """이 점검을 실행한다. 예외를 던지지 않고 결과로 돌려준다."""
