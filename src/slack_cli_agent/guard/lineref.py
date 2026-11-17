"""Catches a reply that refers back to a cited location by its line number.

2026-11-17 private-taeil: a reply listed `PERSONA.md:127` and `PERSONA.md:159`
as labels, then wrote "127 때문에 넓게 확인해야 함" and "159 쪽이 밀렸음" in the
body. The owner reads "127" as an identifier with no meaning attached and has
to scroll back to the label to decode the sentence. The fix is to name the rule
by what it says, so the sentence stands on its own.

Prompting alone can't be relied on here — the draft is written by the same
model that chose the shorthand, and the shorthand looks correct to it because
the label is two lines above. This is a deterministic pattern (a bare number
that also appears as `<file>:<line>` in the same body), so it is checked in
code: first by asking for a rewrite, and if the bare number survives that,
by expanding it back to the full `<file>:<line>` form, which at least says
which file and line it means.
"""

from __future__ import annotations

import logging
import re
from typing import ClassVar

from slack_cli_agent.guard.base import GuardContext, GuardResult, OutputGuard, RerunRequest

log = logging.getLogger(__name__)

# `PERSONA.md:127`, `src/core/pipeline.py:1629`, `bot.py:8249-8255`
LABEL_RE = re.compile(
    r"(?P<label>[\w./-]+\.(?:md|markdown|py|json|jsonl|ya?ml|toml|txt|sh|ts|tsx|js|sql)):(?P<line>\d{1,6})"
)
_CODE_RE = re.compile(r"```.*?```|`[^`\n]*`|<?https?://\S+>?", re.DOTALL)

# A two-digit number standing alone is a measurement far more often than a
# line reference: scanning 50 archived reply files, every bare match below
# this bound was a count or a duration (08-20 의 "1초", "12", "88", "4 커밋"),
# so only three-digit-and-up line numbers are checked.
MIN_LINE = 100
# How far from its label a bare number still reads as that label. The
# 2026-11-17 case had 190 characters between them; a number further off in a
# long reply is its own value, not shorthand for the citation.
MAX_DISTANCE = 1500


def _mask_code(text: str) -> str:
    """Blanks out code spans and URLs, keeping offsets so matches map onto
    the original."""
    return _CODE_RE.sub(lambda m: " " * len(m.group(0)), text)


def _bare_re(line: str) -> re.Pattern[str]:
    # Not part of a longer number, not preceded by `:`/`.`/`/` (already inside
    # a label, a version, or a slash-separated series like "264/360"), not
    # followed by a word char — "127줄" and "127건" carry their own unit and
    # are left alone.
    return re.compile(rf"(?<![\w:.\-/]){re.escape(line)}(?![\w:.\-/])")


def _bare_spans(body: str) -> list[tuple[int, int, str, str]]:
    """Locates every bare line-number reference as (start, end, line, label).

    Offsets are into `body` itself: the match runs on the code-masked copy,
    whose length is unchanged.
    """
    text = body or ""
    labels: dict[str, tuple[str, int]] = {}
    for m in LABEL_RE.finditer(text):
        if int(m.group("line")) < MIN_LINE:
            continue
        labels.setdefault(m.group("line"), (m.group("label") + ":" + m.group("line"), m.start()))
    if not labels:
        return []
    masked = _mask_code(text)
    spans: list[tuple[int, int, str, str]] = []
    for line, (label, at) in labels.items():
        for m in _bare_re(line).finditer(masked):
            if abs(m.start() - at) <= MAX_DISTANCE:
                spans.append((m.start(), m.end(), line, label))
    return sorted(spans)


def find_bare_refs(body: str) -> dict[str, str]:
    """Maps bare line numbers in `body` to the full label they came from.

    Empty when the body has no `<file>:<line>` label, or every mention of
    those numbers stays attached to its label.
    """
    return {line: label for _, _, line, label in _bare_spans(body)}


def expand_bare_refs(body: str) -> str:
    """Rewrites each bare line number back to its full `<file>:<line>` label."""
    text = body or ""
    for start, end, _, label in reversed(_bare_spans(text)):
        text = text[:start] + label + text[end:]
    return text


def bare_line_ref_prompt(body: str, refs: dict[str, str]) -> str:
    listed = ", ".join(f"{line} ({label})" for line, label in sorted(refs.items()))
    return (
        "직전 답변에 줄 번호를 지칭어로 쓴 문장이 있다. 그 문장만 고쳐 답변 전체를 다시 낸다.\n\n"
        f"줄 번호로 지칭한 값 : {listed}\n\n"
        "<draft>\n" + (body or "").strip() + "\n</draft>\n\n"
        "고치는 방법\n"
        "- 그 숫자를 그 위치에 적힌 내용의 이름으로 바꾼다. "
        '예 : "127 때문에 넓게 확인해야 함" 을 '
        '"확인 범위를 전부 적을 수 있어야 한다는 줄 때문에 넓게 확인해야 함" 으로 쓴다\n'
        "- 근거 위치를 같이 보이려면 그 문장과 떨어진 곳에 파일과 줄 번호를 함께 적는다. "
        "본문 문장 안에서는 숫자만으로 그 위치를 부르지 않는다\n"
        "- 고친 문장 밖은 한 글자도 바꾸지 않는다. "
        "말투, 줄바꿈, 수치, 코드블록, 꺾쇠 멘션, 이중 대괄호 표식을 그대로 둔다"
    )


class BareLineRefGuard(OutputGuard):
    """Requests a rewrite when the body refers to a cited location by a bare
    line number, and expands the number to its full label if that fails.
    """

    name: ClassVar[str] = "bare_line_ref"

    def apply(self, body: str, ctx: GuardContext) -> GuardResult:
        refs = find_bare_refs(body)
        if not refs:
            return GuardResult(body=body, changed=False)

        if ctx.is_rewrite_retry:
            expanded = expand_bare_refs(body)
            log.warning("줄 번호 지칭이 재작성 뒤에도 남아 전체 라벨로 되돌린다 : %s", sorted(refs))
            return GuardResult(
                body=expanded,
                changed=expanded != body,
                detail={"bare_line_ref_retry_failed": sorted(refs)},
            )

        return GuardResult(
            body=body,
            changed=False,
            detail={"bare_line_refs": sorted(refs)},
            rerun=RerunRequest(
                reason="bare_line_ref",
                rewrite_prompt=bare_line_ref_prompt(body, refs),
                guard_name=self.name,
            ),
        )
