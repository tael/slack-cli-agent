"""기본 관리 명령 3종.

원본 `handle_admin`(01-source-analysis.md 18절)이 다루는 명령 중 회사
결합이 없는 것만 코어로 옮긴다. "말수 많게/적게", "api 모드", "코치 모드",
"학습 제안/반영/되돌리기" 는 채널 설정 쓰기(JsonStore)와 지식 축적 배치에
의존하는데 그 둘 다 이 작업 범위 밖이라(웨이브 0 의 `config/`, 그리고
`learn.py` 대응 모듈이 아직 없다) 여기서는 만들지 않는다. 채널 쓰기가
생기면 그때 옮긴다.
"""

from __future__ import annotations

import json
from typing import ClassVar

from .command import AdminCommand, AdminContext, AdminResult

_HELP_TEXT = (
    "관리 명령 목록 :\n"
    "- 채널 목록 : 지금 응답하는 채널을 보여준다\n"
    "- 엔진 상태 : 지금 어느 실행기로 도는지 보여준다\n"
    "- 도움말 : 이 안내를 보여준다"
)


class HelpCommand(AdminCommand):
    """도움말. 원본 ADMIN_HELP 를 간추린 것이다."""

    name: ClassVar[str] = "help"

    def matches(self, text: str) -> bool:
        return text.strip() in ("도움말", "help", "명령어")

    def execute(self, ctx: AdminContext) -> AdminResult:
        return AdminResult(message=f"*{ctx.profile.display_name} 관리 명령*\n\n{_HELP_TEXT}")


class ChannelListCommand(AdminCommand):
    """지금 응답 중인 채널 목록. 원본의 "채널 목록" 명령과 대응한다.

    원본은 채널마다 사람이 읽을 이름(`fetch_channel_name` 으로 구한 것)을
    함께 보여주는데, 그 조회는 슬랙 API 몫이라 이 패키지의 `ChannelConfig`
    에는 없다. 여기서는 channel_id 로 대신한다.
    """

    name: ClassVar[str] = "channel_list"

    def matches(self, text: str) -> bool:
        return text.strip() in ("채널 목록", "채널목록", "채널 리스트")

    def execute(self, ctx: AdminContext) -> AdminResult:
        configs = ctx.channels.all()
        if not configs:
            return AdminResult(message="*응답 중인 채널*\n\n등록된 채널이 없습니다.")
        lines = ["*응답 중인 채널*"]
        for channel_id, cfg in configs.items():
            mode = "API 안내" if cfg.mode == "api_helpdesk" else "기본"
            chat = {"active": "많음", "quiet": "적음"}.get(cfg.chat, "보통")
            # 이름이 없을 때만 채널 ID 로 대신한다. 채널 ID 만 나오면 어느
            # 채널인지 사람이 알아볼 수 없다.
            lines.append(f"- {cfg.name or channel_id}")
            lines.append(f"  - 응답 형식 : {mode}, 말수 : {chat}")
        return AdminResult(message="\n".join(lines))


class EngineStatusCommand(AdminCommand):
    """지금 어느 실행기로 도는지, 전환 승인 여부.

    원본의 "엔진 상태" 명령과 대응한다. `engine_state.json` 은 사람이 직접
    열어 보고 되돌리는 경우가 있어 파일로 남는다(03-TRD.md 6절).
    """

    name: ClassVar[str] = "engine_status"

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
        label = {"approved": "승인됨", "denied": "거부됨"}.get(state.get("approval"), "승인 대기")
        return AdminResult(
            message=(
                "*실행기 상태*\n\n"
                f"- 바꾼 실행기 : {state.get('engine')}\n"
                f"- 승인 : {label}\n"
                f"- 사유 : {state.get('detail') or '구독 한도 소진'}"
            )
        )

    @staticmethod
    def _read_state(ctx: AdminContext) -> dict:
        path = ctx.profile.paths.engine_state
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
