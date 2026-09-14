"""가드 목록을 순서대로 적용하는 파이프라인.

원본은 이 보정 로직이 `handle_request` 안에 인라인 200줄 가까이 들어 있었다.
가드마다 클래스로 분리한 것을 여기서 순서대로 실행한다.

감사 기록(`AuditLog`)은 이 클래스의 책임이 아니다 — 무엇이 바뀌었는지는
`PipelineResult.details` 로 돌려주는 것까지만 한다. 감사 로그에 남기는 것은
이 파이프라인을 쓰는 쪽의 몫이다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from slack_cli_agent.guard.base import GuardContext, OutputGuard, RerunRequest


@dataclass(frozen=True)
class PipelineResult:
    """파이프라인 한 회 실행의 결과.

    `rerun` 이 있으면 아직 끝나지 않은 것이다. 호출부가 `rerun.rewrite_prompt`
    로 엔진을 다시 불러 새 본문을 받은 뒤, 그 본문으로 파이프라인을 다시
    돌려야 한다. 엔진을 실제로 부르는 것은 이 클래스의 책임이 아니다 — 이
    작업의 범위 밖이다.
    """

    body: str
    changed: bool
    applied: tuple[str, ...] = ()
    details: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    rerun: RerunRequest | None = None


class GuardPipeline:
    """등록된 가드를 순서대로 적용한다."""

    def __init__(self, guards: Sequence[OutputGuard]) -> None:
        self._guards = list(guards)

    def run(self, body: str, ctx: GuardContext) -> PipelineResult:
        current = body
        applied: list[str] = []
        details: dict[str, Mapping[str, Any]] = {}
        for guard in self._guards:
            result = guard.apply(current, ctx)
            if result.rerun is not None:
                # 다시 써야 하는 가드를 만나면 그 자리에서 멈춘다. 이후 가드는
                # 적용하지 않는다 — 새로 받을 본문에 파이프라인을 처음부터
                # 다시 돌려야 순서가 어긋나지 않는다.
                return PipelineResult(
                    body=current,
                    changed=bool(applied),
                    applied=tuple(applied),
                    details=details,
                    rerun=result.rerun,
                )
            if result.changed:
                applied.append(guard.name)
                details[guard.name] = result.detail
            current = result.body
        return PipelineResult(
            body=current, changed=bool(applied), applied=tuple(applied), details=details
        )
