"""Engine switch approve/deny admin commands.

Writes go through `EngineSwitcher.approve`/`deny` rather than touching
`engine_state.json` directly.
"""

from __future__ import annotations

from typing import ClassVar

from ..engine.switcher import EngineSwitcher
from .command import AdminCommand, AdminContext, AdminResult


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
                f"{ctx.profile.primary_engine.type} 한도가 풀리면 알아서 되돌립니다."
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
        return AdminResult(
            message=(
                f"{fallback} 를 쓰지 않겠습니다. "
                f"{ctx.profile.primary_engine.type} 한도가 풀릴 때까지 한도 안내만 답합니다."
            )
        )
