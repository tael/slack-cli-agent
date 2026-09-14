"""봇별 확장점.

원본은 회사 전용 기능(사내 워크플로 도구 조작, 사내 API 헬프데스크 모드, 조직
코치 모드)이 코어 코드(`bot.py`)에 섞여 있다(01-source-analysis.md 21절).
`BotPlugin` 은 그것을 코어 밖으로 빼는 유일한 통로다 — 전부 기본 구현이
빈 튜플이라, 플러그인은 자기가 필요한 확장점만 재정의한다.
"""

from __future__ import annotations

from abc import ABC
from collections.abc import Sequence
from typing import ClassVar, TYPE_CHECKING

if TYPE_CHECKING:
    from ..admin.command import AdminCommand
    from ..auth.policy import AccessExtension
    from ..guard.base import OutputGuard
    from ..preflight.check import PreflightCheck
    from ..prompt.sections import PromptSection


class BotPlugin(ABC):
    """봇 하나에 붙는 조직 전용 확장 전부의 진입점."""

    name: ClassVar[str]

    def access_extensions(self) -> Sequence["AccessExtension"]:
        return ()

    def admin_commands(self) -> Sequence["AdminCommand"]:
        return ()

    def prompt_sections(self) -> Sequence["PromptSection"]:
        return ()

    def output_guards(self) -> Sequence["OutputGuard"]:
        return ()

    def preflight_checks(self) -> Sequence["PreflightCheck"]:
        return ()
