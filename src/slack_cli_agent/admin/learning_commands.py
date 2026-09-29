"""Learning suggest/apply/revert admin commands.

Judgment and message text live entirely in `LearningService`; this layer
only forwards the body to it. The service is assembled per call rather than
injected, the same way other admin commands pull what they need from
`ctx.profile` — the command registry doesn't need to know any paths.
"""

from __future__ import annotations

from typing import ClassVar

from ..config.paths import StatePaths
from ..learning.apply import LearningApplier, LearningReverter
from ..learning.proposal import ProposalStore
from ..learning.render import ProposalRenderer
from ..learning.service import LearningService
from .command import AdminCommand, AdminContext, AdminResult


def _service(ctx: AdminContext) -> LearningService:
    paths = StatePaths(ctx.profile.state_dir)
    return LearningService(
        store=ProposalStore(paths.proposals),
        applier=LearningApplier(paths.learned, ctx.profile.display_name),
        reverter=LearningReverter(paths.learned),
        renderer=ProposalRenderer(),
    )


class LearningShowCommand(AdminCommand):
    name: ClassVar[str] = "learning_show"
    usage: ClassVar[str] = "학습 제안"
    description: ClassVar[str] = "최근 배치가 만든 지식 갱신안을 보여준다"

    def matches(self, text: str) -> bool:
        return text.strip() in ("학습 제안", "학습제안", "배운 거")

    def execute(self, ctx: AdminContext) -> AdminResult:
        return AdminResult(message=_service(ctx).show_proposal())


class LearningApplyCommand(AdminCommand):
    name: ClassVar[str] = "learning_apply"
    usage: ClassVar[str] = "학습 반영"
    description: ClassVar[str] = "그 갱신안을 지식 파일에 반영한다"

    def matches(self, text: str) -> bool:
        return text.strip() in ("학습 반영", "학습반영", "배운 거 반영")

    def execute(self, ctx: AdminContext) -> AdminResult:
        return AdminResult(message=_service(ctx).apply_latest())


class LearningRevertCommand(AdminCommand):
    """Reverts what was applied on a given day.

    Takes the last whitespace-separated token as the date and lets the
    service validate its format — if no date is given, that token is the
    command word itself, and the service reports the format error instead.
    """

    name: ClassVar[str] = "learning_revert"
    usage: ClassVar[str] = "학습 되돌리기 YYYY-MM-DD"
    description: ClassVar[str] = "그날 반영한 줄만 지운다"

    def matches(self, text: str) -> bool:
        return text.strip().startswith(("학습 되돌리기", "학습되돌리기"))

    def execute(self, ctx: AdminContext) -> AdminResult:
        조각 = ctx.text.split()
        return AdminResult(message=_service(ctx).revert(조각[-1] if 조각 else ""))
