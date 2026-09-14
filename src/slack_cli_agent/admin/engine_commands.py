"""엔진 전환 승인/거부 관리 명령.

원본 `handle_admin` 의 "엔진 승인"/"엔진 거부" 분기와 대응한다. 전환 상태
쓰기는 `EngineSwitcher` 를 거친다 — `engine_state.json` 을 직접 읽고 쓰지
않는다(이미 `EngineStatusCommand` 가 상태 읽기만 하고 있어, 쓰기는 여기서
`EngineSwitcher.approve`/`deny` 로 맡긴다).
"""

from __future__ import annotations

from typing import ClassVar

from ..engine.switcher import EngineSwitcher
from .command import AdminCommand, AdminContext, AdminResult


class EngineApproveCommand(AdminCommand):
    """대체 실행기로의 전환을 승인한다. 전환이 없으면 그렇다고 답한다."""

    name: ClassVar[str] = "engine_approve"

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
    """대체 실행기로의 전환을 거부한다. 전환이 없으면 그렇다고 답한다."""

    name: ClassVar[str] = "engine_deny"

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
