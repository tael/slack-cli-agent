"""허용 도구 결정.

원본 build_command 의 분기를 그대로 옮긴다.

    readonly(부검·디버그 추적)  ALLOWED_TOOLS 만
    소유자                     ALLOWED_TOOLS + OWNER_EXTRA_TOOLS
    확장이 적용되는 자리        ALLOWED_TOOLS + 확장이 주는 도구
    그 외                      ALLOWED_TOOLS 만

읽기 전용이 기본이다. Skill 은 aside(부검·디버그·서식 점검) 가 아니고
채널이 켜 두었을 때만 맨 끝에 더한다.
"""

from __future__ import annotations

from collections.abc import Sequence

from .policy import AccessExtension
from .principal import Principal, TrustLevel

SKILL_TOOL = "Skill"


class ToolPolicy:
    """요청 하나에 붙일 도구 목록을 정한다."""

    def __init__(
        self,
        base_tools: Sequence[str],
        owner_tools: Sequence[str] = (),
        extensions: Sequence[AccessExtension] = (),
    ) -> None:
        self._base_tools = tuple(base_tools)
        self._owner_tools = tuple(owner_tools)
        self._extensions = tuple(extensions)

    def tools_for(
        self,
        principal: Principal,
        *,
        prompt: str = "",
        readonly: bool = False,
        aside: bool = False,
        skills_enabled: bool = False,
    ) -> str:
        tools: list[str] = list(self._base_tools)
        if readonly:
            pass
        elif principal.trust is TrustLevel.OWNER:
            tools += self._owner_tools
        else:
            for ext in self._extensions:
                if ext.applies(principal, prompt):
                    tools += list(ext.extra_tools(principal))
        if not aside and skills_enabled:
            tools.append(SKILL_TOOL)
        return ",".join(tools)
