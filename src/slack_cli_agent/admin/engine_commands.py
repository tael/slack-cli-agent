"""Engine switch approve/deny admin commands.

Writes go through `EngineSwitcher.approve`/`deny` rather than touching
`engine_state.json` directly.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import ClassVar

from ..engine.switcher import EngineSwitcher
from .command import AdminCommand, AdminContext, AdminResult


class ModelChangeCommand(AdminCommand):
    """Owner-only, allowlisted model change with an explicit full restart."""

    name: ClassVar[str] = "model_change"
    usage: ClassVar[str] = "모델 변경 <허용 모델>"
    description: ClassVar[str] = "소유자 모델을 바꾸고 ingress·worker를 재기동한다"
    _PREFIX: ClassVar[str] = "모델 변경 "

    def matches(self, text: str) -> bool:
        return text.strip().startswith(self._PREFIX)

    def execute(self, ctx: AdminContext) -> AdminResult:
        model = ctx.text.strip()[len(self._PREFIX):].strip()
        allowed = ctx.profile.primary_engine.allowed_models
        if not allowed:
            return AdminResult("허용 모델 목록이 설정되지 않아 변경하지 않았습니다.", applied=False)
        if model not in allowed:
            return AdminResult(
                "허용되지 않은 모델이라 변경하지 않았습니다. 허용 모델: " + ", ".join(allowed),
                applied=False,
            )
        if not ctx.profile.source_file:
            return AdminResult("프로필 파일 경로를 확인하지 못해 변경하지 않았습니다.", applied=False)
        labels = ctx.profile.service_labels
        if len(labels) != 2 or not all(label.startswith("local.") for label in labels):
            return AdminResult("재기동 서비스 목록이 올바르지 않아 변경하지 않았습니다.", applied=False)
        try:
            data = json.loads(ctx.profile.source_file.read_text(encoding="utf-8"))
            engine = data["primary_engine"]
            engine["model"] = model
            engine["model_owner"] = model
            temporary = ctx.profile.source_file.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(ctx.profile.source_file)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return AdminResult(f"프로필을 바꾸지 못했습니다: {exc}", applied=False)
        subprocess.Popen(
            [
                sys.executable, "-m", "slack_cli_agent.admin.delayed_restart",
                str(ctx.profile.source_file), str(ctx.profile.paths.root / "model_change.json"), model, *labels,
            ],
            start_new_session=True,
            close_fds=True,
        )
        return AdminResult(
            f"모델을 {model}로 저장했고 ingress·worker 재기동을 예약했습니다. "
            "다음 엔진 상태 명령에서 설정 모델과 재기동 결과를 확인할 수 있습니다."
        )


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
