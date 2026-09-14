"""학습 제안·반영·되돌리기 관리 명령.

원본 `handle_admin` 의 학습 3종 분기와 대응한다. 판정과 문구는 전부
`LearningService` 가 맡고 이 계층은 본문을 그 서비스로 넘기기만 한다 —
`learning/service.py` 의 세 메서드가 원본 세 함수와 1:1 로 대응한다.

서비스를 생성자로 주입받지 않고 실행 시점에 조립한다. 다른 관리 명령이
`ctx.profile` 에서 필요한 것을 만드는 방식과 같다 — 명령 등록부가 경로를
알 필요가 없다.
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
        applier=LearningApplier(paths.knowledge, ctx.profile.display_name),
        reverter=LearningReverter(paths.knowledge),
        renderer=ProposalRenderer(),
    )


class LearningShowCommand(AdminCommand):
    """가장 최근 학습 제안을 보여준다. 아직 반영하지는 않는다."""

    name: ClassVar[str] = "learning_show"

    def matches(self, text: str) -> bool:
        return text.strip() in ("학습 제안", "학습제안", "배운 거")

    def execute(self, ctx: AdminContext) -> AdminResult:
        return AdminResult(message=_service(ctx).show_proposal())


class LearningApplyCommand(AdminCommand):
    """최근 제안을 지식 파일에 반영한다. 같은 항목은 두 번 들어가지 않는다."""

    name: ClassVar[str] = "learning_apply"

    def matches(self, text: str) -> bool:
        return text.strip() in ("학습 반영", "학습반영", "배운 거 반영")

    def execute(self, ctx: AdminContext) -> AdminResult:
        return AdminResult(message=_service(ctx).apply_latest())


class LearningRevertCommand(AdminCommand):
    """그날 반영한 줄을 지운다. 날짜를 본문에서 뽑는다.

    원본은 `cmd.split()[-1]` 로 마지막 토막을 날짜로 봤다. 날짜 형식 검사는
    서비스가 하므로 여기서는 마지막 토막만 넘긴다 — 날짜를 안 붙이면
    명령어 자체가 마지막 토막이 되고, 그것이 날짜 형식이 아니라 서비스가
    형식 안내를 낸다.
    """

    name: ClassVar[str] = "learning_revert"

    def matches(self, text: str) -> bool:
        return text.strip().startswith(("학습 되돌리기", "학습되돌리기"))

    def execute(self, ctx: AdminContext) -> AdminResult:
        조각 = ctx.text.split()
        return AdminResult(message=_service(ctx).revert(조각[-1] if 조각 else ""))
