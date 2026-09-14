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

__all__ = ["ELAPSED_LINE", "ELAPSED_MODEL_LINE"]
