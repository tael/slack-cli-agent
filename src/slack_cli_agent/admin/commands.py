"""Core admin commands."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from .command import AdminCommand, AdminContext, AdminResult


class HelpCommand(AdminCommand):
    name: ClassVar[str] = "help"
    usage: ClassVar[str] = "도움말"
    description: ClassVar[str] = "이 안내를 보여준다"

    def matches(self, text: str) -> bool:
        return text.strip() in ("도움말", "help", "명령어")

    def execute(self, ctx: AdminContext) -> AdminResult:
        return AdminResult(
            message=(
                f"*{ctx.profile.display_name} 관리 명령*\n"
                "소유자만 쓸 수 있습니다.\n\n"
                f"{ctx.help_text}\n\n"
                "새 채널은 초대 후 소유자가 부르면 자동 등록됩니다."
            )
        )


# Channel mode as the user sees it. The stored value is a code-internal
# name and shouldn't reach the user.
_MODE_LABELS = {"": "기본", "agent_coach": "코치", "private": "비공개"}


class ChannelListCommand(AdminCommand):
    """Lists channels currently being responded to.

    Uses channel_id in place of a human-readable name, since that lookup is
    a Slack API call this package's `ChannelConfig` doesn't make.
    """

    name: ClassVar[str] = "channel_list"
    usage: ClassVar[str] = "채널 목록"
    description: ClassVar[str] = "지금 응답하는 채널을 보여준다"

    def matches(self, text: str) -> bool:
        return text.strip() in ("채널 목록", "채널목록", "채널 리스트")

    def execute(self, ctx: AdminContext) -> AdminResult:
        configs = ctx.channels.all()
        if not configs:
            return AdminResult(message="*응답 중인 채널*\n\n등록된 채널이 없습니다.")
        lines = ["*응답 중인 채널*"]
        for channel_id, cfg in configs.items():
            chat = {"active": "많음", "quiet": "적음"}.get(cfg.chat, "보통")
            lines.append(f"- {cfg.name or channel_id}")
            mode = _MODE_LABELS.get(cfg.mode, cfg.mode or "기본")
            lines.append(f"  - 응답 형식 : {mode}, 말수 : {chat}")
        return AdminResult(message="\n".join(lines))


class EngineStatusCommand(AdminCommand):
    """Shows which engine is active and the fallback approval state.

    `engine_state.json` is a real file since people sometimes open and
    revert it by hand.
    """

    name: ClassVar[str] = "engine_status"
    usage: ClassVar[str] = "엔진 상태"
    description: ClassVar[str] = "지금 어느 실행기로 도는지, 전환 승인 여부를 보여준다"

    def matches(self, text: str) -> bool:
        return text.strip() in ("엔진 상태", "엔진상태", "엔진")

    def execute(self, ctx: AdminContext) -> AdminResult:
        state = self._read_state(ctx)
        if not state:
            fallback = ctx.profile.fallback_engine.type if ctx.profile.fallback_engine else "(없음)"
            return AdminResult(
                message=(
                    "*실행기 상태*\n\n"
                    f"- 지금 : {ctx.profile.primary_engine.type}\n"
                    f"- 대체 : {fallback}\n"
                    "- 전환 : 없음"
                )
            )
        approval = str(state.get("approval") or "")
        label = {"approved": "승인됨", "denied": "거부됨"}.get(approval, "승인 대기")
        return AdminResult(
            message=(
                "*실행기 상태*\n\n"
                f"- 바꾼 실행기 : {state.get('engine')}\n"
                f"- 승인 : {label}\n"
                f"- 사유 : {state.get('detail') or '구독 한도 소진'}"
            )
        )

    @staticmethod
    def _read_state(ctx: AdminContext) -> dict[str, Any]:
        path = ctx.profile.paths.engine_state
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}
