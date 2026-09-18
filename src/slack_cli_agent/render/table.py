"""Pipe-table builder for Slack messages.

Lives here rather than in review/ because the slow-request report builds the
same tables, and importing it from review/ put observability on the review
package -- a cycle the moment review needed to report a slow run (sca-xck).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def cell(value: Any) -> str:
    # A stray pipe or newline would break the table row it's in and everything after it.
    return str(value).replace("|", "/").replace("\n", " ").strip() or "-"


def as_table(rows: Sequence[tuple[Any, ...]], head: tuple[str, ...] = ("항목", "값")) -> str:
    if not rows:
        return ""
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for row in rows:
        lines.append("| " + " | ".join(cell(c) for c in row) + " |")
    return "\n".join(lines)
