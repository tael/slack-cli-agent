"""Regexes for trailer markers appended to message bodies before sending.

Lives in `core` (not `guard`) so the slack layer doesn't need to import guard.
`guard/watch.py` re-exports these to keep the old import path working.
"""

from __future__ import annotations

import re

# Trailing marker, not part of the reply body — strip it before follow-up-question detection.
ELAPSED_LINE = re.compile(r"(?:\n\s*>\s*걸린 시간 : \s*\d+초\s*)+$")
# Models sometimes echo this line back from earlier turns; strip it too. Also matches the old "실행 엔진" label.
ELAPSED_MODEL_LINE = re.compile(r"\n*>\s*실행\s*(모델|엔진)\s*:.*$", re.MULTILINE)

# The model's own mark that this turn is waiting on a human decision
# (bot.py:1507, 2026-09-19). A short approval ("좋아", "네", "ㅇㅇ") reads the
# same as a reaction; the only thing that tells them apart is whether the
# bot's own last turn actually asked for one, and that fact has to survive a
# restart since the decision can land hours later (see session/awaiting.py).
ASK_TAG = "[[ASK]]"
# Only a line that is nothing but the tag counts. bot.py:1510-1512 (2026-11-01):
# matching the tag anywhere, including mid-sentence, ate real words next to it
# ("제가를 붙인", "내거나로") when a reply merely mentioned the convention.
ASK_RE = re.compile(r"^[ \t]*\[\[ASK\]\][ \t]*(?:\n|$)", re.MULTILINE)


def take_ask_tag(text: str) -> tuple[str, bool]:
    """Strips the `[[ASK]]` line and reports whether it was there.

    Matches bot.py:8273-8278's `take_ask_tag`.
    """
    if not text:
        return text, False
    stripped = ASK_RE.sub("", text)
    return stripped.rstrip(), stripped != text


__all__ = ["ASK_RE", "ASK_TAG", "ELAPSED_LINE", "ELAPSED_MODEL_LINE", "take_ask_tag"]
