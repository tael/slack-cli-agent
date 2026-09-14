"""관리 명령 라우팅.

`matches` 로 맞는 명령을 고르고, 권한은 여기서 한 곳에서만 대조한다.
명령 클래스마다 권한 검사를 반복해 적지 않는다 — 하나를 빠뜨리면 그
명령만 조용히 뚫린다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from .command import AdminCommand, AdminContext, AdminResult


class AdminRouter:
    """등록된 명령 중 본문에 맞는 것을 찾아 실행한다."""

    def __init__(self, commands: Sequence[AdminCommand]) -> None:
        self._commands = tuple(commands)

    def dispatch(self, text: str, ctx: AdminContext) -> AdminResult | None:
        """맞는 명령이 없으면 None. 일반 요청으로 넘어간다.

        맞는 명령은 있으나 권한이 모자라면 실행하지 않고 거절 결과를
        돌려준다 — None 을 주면 호출부가 일반 요청으로 다시 처리해
        모델에게 흘러갈 수 있다.
        """
        for command in self._commands:
            if not command.matches(text):
                continue
            if ctx.principal.trust < command.required_trust:
                return AdminResult(
                    message="이 명령은 권한이 없어 실행할 수 없습니다.",
                    handled=False,
                )
            # 본문을 여기서 채운다. 맥락을 만드는 호출부마다 넣게 하면
            # 한 곳만 빠뜨려도 그 경로의 명령이 인자를 못 읽는다.
            return command.execute(replace(ctx, text=text))
        return None
