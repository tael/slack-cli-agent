"""Core admin commands."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
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
_MODE_LABELS = {"": "기본", "agent_coach": "코치", "api_helpdesk": "연동 API", "private": "비공개"}

# Session scope as the user sees it, same reason as _MODE_LABELS.
_SCOPE_LABELS = {"channel": "채널", "thread": "스레드"}


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
            # Without these two lines there's no way to see from Slack which
            # channels answer without being named, or which keep one session
            # across the whole channel.
            called = "없어도 답함" if cfg.answer_unaddressed else "불러야 답함"
            scope = _SCOPE_LABELS.get(cfg.session_scope, cfg.session_scope)
            lines.append(f"  - 호명 : {called}, 세션 : {scope}")
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
        model_change = self._read_model_change(ctx)
        model_lines = [f"- 설정 모델 : {ctx.profile.primary_engine.model_for_owner()}"]
        if model_change:
            services = model_change.get("services")
            service_text = (
                "완료" if model_change.get("complete") else "실패 또는 진행 중"
            )
            if isinstance(services, dict) and services:
                service_text += " (" + ", ".join(
                    f"{name}: {'정상' if ok else '실패'}" for name, ok in services.items()
                ) + ")"
            model_lines.append(f"- 모델 변경 재기동 : {service_text}")
        if not state:
            fallback = ctx.profile.fallback_engine.type if ctx.profile.fallback_engine else "(없음)"
            return AdminResult(
                message=(
                    "*실행기 상태*\n\n"
                    f"- 지금 : {ctx.profile.primary_engine.type}\n"
                    + "\n".join(model_lines) + "\n"
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
                + "\n".join(model_lines) + "\n"
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

    @staticmethod
    def _read_model_change(ctx: AdminContext) -> dict[str, Any]:
        path = ctx.profile.paths.root / "model_change.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

class InflightStatusCommand(AdminCommand):
    """Lists the requests being handled right now (B51).

    Reads the state snapshot the worker writes, not memory: the worker's
    in-flight registry lives in another process and the socket side can't
    see it -- the same cross-process path EngineStatusCommand takes for
    engine_state.json. Elapsed is as of the snapshot write, so the freshness
    line names how old that read is.
    """

    name: ClassVar[str] = "inflight"
    usage: ClassVar[str] = "처리 현황"
    description: ClassVar[str] = "지금 처리 중인 요청(채널·ts·경과)을 보여준다"

    def __init__(self, now: Callable[[], float] = time.time) -> None:
        self._now = now

    def matches(self, text: str) -> bool:
        return text.strip() in ("처리 현황", "처리현황", "진행 중", "진행중")

    def execute(self, ctx: AdminContext) -> AdminResult:
        snap = self._read_snapshot(ctx)
        if snap is None:
            return AdminResult(message="*처리 현황*\n\n상태 파일이 아직 없습니다.")
        active = snap.get("active") or []
        if not active:
            return AdminResult(message="*처리 현황*\n\n지금 처리 중인 요청이 없습니다.")
        lines = ["*처리 현황*", ""]
        for ev in active:
            channel = ev.get("channel") or "?"
            ts = ev.get("ts") or "?"
            elapsed = ev.get("elapsed_sec")
            if isinstance(elapsed, int | float):
                lines.append(f"- {channel}:{ts} (경과 {round(float(elapsed))}초)")
            else:
                lines.append(f"- {channel}:{ts}")
        written_at = snap.get("written_at")
        if isinstance(written_at, int | float):
            age = max(0, round(self._now() - float(written_at)))
            lines.append("")
            lines.append(f"({age}초 전 기준)")
        return AdminResult(message="\n".join(lines))

    @staticmethod
    def _read_snapshot(ctx: AdminContext) -> dict[str, Any] | None:
        path = ctx.profile.paths.state_snapshot
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None
