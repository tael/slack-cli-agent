"""설정한 문구로 시작하는 줄을 지운다.

원본 `drop_vooster()` 를 일반화했다. 원본은 조직 텔레메트리 인사 줄
하나(`VOOSTER_HEAD`)만 걷어냈는데, 그 문구는 조직 고유값이라 이 코드베이스에
그대로 옮길 수 없다. 대신 지울 문구 목록을 `RuntimeSettings.dropped_line_heads`
로 받는 범용 가드로 만든다 — 목록이 여러 개일 수 있고, 기본값은 빈 목록이다.

줄 단위로만 판단한다. 본문 중간에 그 문구가 인용으로 들어온 경우에는
그 줄만 지우면 문맥이 깨지므로, 그 줄이 그 문구로 시작할 때만 지운다.
앞에 붙는 대시(—, –, -)와 지운 뒤 남는 빈 줄까지 함께 걷어낸다.
"""

from __future__ import annotations

from typing import ClassVar

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.guard.base import GuardContext, GuardResult, OutputGuard


class ConfiguredLineDropGuard(OutputGuard):
    """`dropped_line_heads` 에 있는 문구로 시작하는 줄을 지운다."""

    name: ClassVar[str] = "dropped_line"

    def __init__(self, settings: RuntimeSettings) -> None:
        self._heads = tuple(settings.dropped_line_heads)

    def apply(self, body: str, ctx: GuardContext) -> GuardResult:
        if not self._heads:
            return GuardResult(body=body, changed=False)
        if not any(head in body for head in self._heads):
            return GuardResult(body=body, changed=False)

        dropped: list[str] = []
        out: list[str] = []
        for line in body.split("\n"):
            bare = line.strip().lstrip("—-–").strip()
            matched = next((head for head in self._heads if bare.startswith(head)), None)
            if matched:
                dropped.append(matched)
                continue
            out.append(line)

        if not dropped:
            return GuardResult(body=body, changed=False)

        new_body = "\n".join(out).rstrip()
        return GuardResult(body=new_body, changed=True, detail={"dropped": dropped})
