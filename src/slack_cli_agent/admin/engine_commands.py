"""Engine switch approve/deny admin commands.

Writes go through `EngineSwitcher.approve`/`deny` rather than touching
`engine_state.json` directly.
"""

from __future__ import annotations

from typing import ClassVar

from ..engine.switcher import EngineSwitcher
from .command import AdminCommand, AdminContext, AdminResult


def _recovery_note(switcher: EngineSwitcher, primary: str) -> str:
    """전환 계기별로 1차로 돌아오는 조건이 다르다.

    인증 실패를 "한도가 풀리면" 으로 안내하면 기다리면 되는 것으로 읽혀
    아무도 다시 로그인하지 않는다.
    """
    if switcher.reason() == EngineSwitcher.AUTH_FAILURE:
        return f"{primary} 에 다시 로그인하면 알아서 되돌립니다."
    return f"{primary} 한도가 풀리면 알아서 되돌립니다."


class EngineApproveCommand(AdminCommand):
    name: ClassVar[str] = "engine_approve"
    usage: ClassVar[str] = "엔진 승인"
    description: ClassVar[str] = "한도로 바뀐 실행기로 답하는 것을 허용한다"

    def matches(self, text: str) -> bool:
        return text.strip() in ("엔진 승인", "엔진승인", "엔진 허용")

    def execute(self, ctx: AdminContext) -> AdminResult:
        switcher = EngineSwitcher(ctx.profile.paths.engine_state)
        if not switcher.is_switched():
            return AdminResult(
                message=f"지금은 {ctx.profile.primary_engine.type} 로 돌고 있어요. 승인할 전환이 없습니다."
            )
        switcher.approve()
        fallback = ctx.profile.fallback_engine.type if ctx.profile.fallback_engine else "(없음)"
        return AdminResult(
            message=(
                f"{fallback} 로 답하겠습니다. "
                f"{_recovery_note(switcher, ctx.profile.primary_engine.type)}"
            )
        )


class EngineDenyCommand(AdminCommand):
    name: ClassVar[str] = "engine_deny"
    usage: ClassVar[str] = "엔진 거부"
    description: ClassVar[str] = "바뀐 실행기로 답하지 않고 한도 안내만 낸다"

    def matches(self, text: str) -> bool:
        return text.strip() in ("엔진 거부", "엔진거부", "엔진 취소")

    def execute(self, ctx: AdminContext) -> AdminResult:
        switcher = EngineSwitcher(ctx.profile.paths.engine_state)
        if not switcher.is_switched():
            return AdminResult(
                message=f"지금은 {ctx.profile.primary_engine.type} 로 돌고 있어요. 거부할 전환이 없습니다."
            )
        switcher.deny()
        fallback = ctx.profile.fallback_engine.type if ctx.profile.fallback_engine else "(없음)"
        primary = ctx.profile.primary_engine.type
        until = (f"{primary} 에 다시 로그인할 때까지"
                 if switcher.reason() == EngineSwitcher.AUTH_FAILURE
                 else f"{primary} 한도가 풀릴 때까지")
        return AdminResult(
            message=f"{fallback} 를 쓰지 않겠습니다. {until} 안내만 답합니다."
        )
