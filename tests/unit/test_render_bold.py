"""Bold spans that Slack's markdown block would leave as literal ** (2026-11-27).

"**8%**였고" rendered with the asterisks visible: the closing ** sits after
punctuation and before a letter, so CommonMark does not treat it as closing.
"""

from __future__ import annotations

import pytest

from slack_cli_agent.render.bold import fix_bold_flanking


@pytest.mark.parametrize(
    ("src", "want"),
    [
        ("일반 품질 룰은 **8%**였고", "일반 품질 룰은 **8**%였고"),
        ("지적 90개 중 **94%**는 룰에 없던", "지적 90개 중 **94**%는 룰에 없던"),
        ("값**(괄호)**임", "값(**괄호**)임"),
        ("**1.2TB → 85GB.**로", "**1.2TB → 85GB**.로"),
    ],
)
def test_굵게가_깨지는_위치는_문장부호를_밖으로_뺀다(src: str, want: str) -> None:
    assert fix_bold_flanking(src) == want


@pytest.mark.parametrize(
    "src",
    [
        "**8%** 수준",
        "**매시 20분 증분 MERGE** 로",
        "**재시도 정책**: maxAttempts 1",
        "**옵션 2로 진행할까요?**",
        "스캔량은 **133GB → 9.9GB** 로 줄어",
        "```\n**8%**였고\n```",
    ],
)
def test_렌더되는_굵게와_코드블록은_그대로_둔다(src: str) -> None:
    assert fix_bold_flanking(src) == src


def test_문장부호만_굵게면_굵게를_뺀다() -> None:
    assert fix_bold_flanking("값**%**였고") == "값%였고"
