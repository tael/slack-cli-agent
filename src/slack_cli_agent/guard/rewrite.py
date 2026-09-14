"""재작성 손실 검출.

원본 `late_rewrite_lost_content` 본문을 그대로 옮겼다. 상한값은 원본이
모듈 상수(`LATE_REWRITE_MIN_RATIO`, `LATE_REWRITE_MIN_CHARS`)였는데, 여기서는
`RuntimeSettings` 에서 주입받는다 — 정책은 데이터, 판정은 객체(TRD P4).
"""

from __future__ import annotations

from typing import ClassVar

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.guard.base import GuardContext, GuardResult, OutputGuard


class RewriteLossGuard(OutputGuard):
    """재작성본이 앞 답의 내용을 잃었는지 본다.

    모델에게 지시만 해서는 막히지 않는다. 코드가 한 번 더 본다. 앞 답이
    충분히 길었는데 재작성본이 그보다 크게 짧아졌으면 잃은 것으로 본다.

    2026-09-11 사고에서 2,000자 넘는 목록이 54자 안내 한 줄로 바뀌었다.
    유실로 판정되면 앞 답을 버리지 않는다 — 사람이 본 적 없는 내용이라
    버리면 그대로 사라진다. 뒤에 이어 붙여 둘 다 낸다(원본 `handle_request`
    가 하던 병합을 여기로 옮겼다).

    `ctx.previous_body` 가 없으면 재작성 상황이 아니므로 판정하지 않는다.
    """

    name: ClassVar[str] = "rewrite_loss"

    def __init__(self, settings: RuntimeSettings) -> None:
        self._min_ratio = settings.late_rewrite_min_ratio
        self._min_chars = settings.late_rewrite_min_chars

    def _lost(self, before: str, after: str) -> bool:
        """원본 `late_rewrite_lost_content` 본문 그대로."""
        before = (before or "").strip()
        after = (after or "").strip()
        if len(before) < self._min_chars:
            return False
        return len(after) < len(before) * self._min_ratio

    def apply(self, body: str, ctx: GuardContext) -> GuardResult:
        before = ctx.previous_body
        if before is None:
            return GuardResult(body=body, changed=False)
        if not self._lost(before, body):
            return GuardResult(body=body, changed=False)
        merged = f"{before.rstrip()}\n\n{(body or '').strip()}"
        return GuardResult(
            body=merged,
            changed=True,
            detail={
                "lost": True,
                "before_chars": len(before.strip()),
                "after_chars": len((body or "").strip()),
            },
        )
