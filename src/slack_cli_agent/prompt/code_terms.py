"""Korean wording for English code terms the bot keeps translating literally.

2026-11-12: 297 sentences the figurative corrector rewrote in 139 replies
(11-07 - 11-12) included 61 with 탈출구 and 38 with 형제 호출. Both are the
bot's word-for-word reading of this repository's own docstrings ("Escape
hatches:", "sibling tool_use lines" in guard/pretool/serial_read_guard.py),
and the corrector replaced them the same way nearly every time (탈출구 → 예외
54 of 61). The table is used twice: WorkStyleSection tells the answering
model the standard wording, and guard/figurative.py replaces what still gets
through with no model call.
"""

from __future__ import annotations

#: (literal translation, standard wording). Only pairs the corrector rewrote
#: one way: a word whose fix depends on context (붙다, 칸, 닿다) stays with
#: the model.
CODE_TERMS: tuple[tuple[str, str], ...] = (
    ("형제 호출", "같은 응답의 다른 호출"),
    ("탈출구", "예외"),
)

CODE_TERM_NOTE = (
    "코드 주석과 식별자의 영어 비유어를 한국어로 직역하지 않는다. 표준 용어로 쓴다. "
    "escape hatch 는 예외, sibling tool call 은 같은 응답의 다른 호출, backstop 은 최종 차단, "
    "guard rail 은 제한 조건이다"
)


def replace_code_terms(text: str) -> tuple[str, list[dict[str, str]]]:
    """(text with CODE_TERMS replaced, one fix row per term found)."""
    fixes: list[dict[str, str]] = []
    for literal, standard in CODE_TERMS:
        if literal in text:
            text = text.replace(literal, standard)
            fixes.append({"before": literal, "after": standard, "by": "code_terms"})
    return text, fixes
