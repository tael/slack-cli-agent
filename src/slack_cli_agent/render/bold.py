"""Keeps **bold** renderable in Slack markdown blocks.

CommonMark closes ** only when it is right-flanking: a closing ** right after
punctuation must be followed by whitespace or punctuation. "**8%**였고" fails
that test and Slack shows the asterisks (2026-11-27). Moving the edge
punctuation outside the span keeps the text and makes the delimiters valid.
"""

from __future__ import annotations

import re

_SPAN = re.compile(r"\*\*([^*\n]+?)\*\*")
_FENCE = re.compile(r"(```.*?```)", re.DOTALL)


def _is_punct(ch: str) -> bool:
    return not ch.isalnum() and not ch.isspace()


def _fix_span(text: str, m: re.Match[str]) -> str:
    inner = m.group(1)
    before = text[m.start() - 1] if m.start() else ""
    after = text[m.end()] if m.end() < len(text) else ""
    lead = trail = ""
    if before.isalnum() and inner and _is_punct(inner[0]):
        cut = next((i for i, c in enumerate(inner) if not _is_punct(c)), len(inner))
        lead, inner = inner[:cut], inner[cut:]
    if after.isalnum() and inner and _is_punct(inner[-1]):
        cut = next((i for i in range(len(inner) - 1, -1, -1) if not _is_punct(inner[i])), -1) + 1
        inner, trail = inner[:cut], inner[cut:]
    if not lead and not trail:
        return m.group(0)
    if not inner.strip():
        return lead + inner + trail
    return f"{lead}**{inner}**{trail}"


def _fix_plain(text: str) -> str:
    return _SPAN.sub(lambda m: _fix_span(text, m), text)


def fix_bold_flanking(text: str) -> str:
    """Moves edge punctuation out of bold spans Slack would not render."""
    if "**" not in text:
        return text
    parts = _FENCE.split(text)
    return "".join(p if p.startswith("```") else _fix_plain(p) for p in parts)
