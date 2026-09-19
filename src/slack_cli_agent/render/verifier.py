"""Validates split chunks before sending and provides a safe fallback."""

from __future__ import annotations

import re
from dataclasses import dataclass

from slack_cli_agent.config.settings import RuntimeSettings

from .blocks import SPLIT_MARKER
from .splitter import split_by_block_budget

TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")
#: A GFM table separator row. The pipe may be followed by spaces and the
#: alignment colons are optional, so "|-" as a prefix test rejects the form
#: models actually write -- "| --- | --- |" (sca-3pr).
TABLE_DIVIDER = re.compile(r"^\|?(\s*:?-+:?\s*\|)+\s*:?-*:?\s*\|?\s*$")
FENCE_LINE = re.compile(r"^\s*```")


@dataclass(frozen=True)
class SplitProblem:
    """One verification failure, with enough of the source to reproduce it.

    The reason alone was not enough: a chunk that fails verification goes
    through safe_fallback, so the thread keeps only the downgraded output and
    the original markdown is gone. Investigating one meant guessing (sca-9uj).
    """

    reason: str
    line_no: int | None = None
    excerpt: str = ""


def _outside_fences(part: str) -> list[tuple[int, str]]:
    """Lines Slack renders as markdown, each with its 1-based line number.

    The number is what ties a problem back to the source; without it the
    caller can only report which chunk failed, not where.
    """
    kept, in_fence = [], False
    for no, line in enumerate(part.split("\n"), start=1):
        if FENCE_LINE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence and line.strip():
            kept.append((no, line))
    return kept


def _header_less_rows(text: str) -> list[tuple[int, str]]:
    """Rows that open a table with no divider line following them.

    Lines inside a fence are dropped first: a table shown as an example there
    is not a table Slack renders (sca-a3b).
    """
    lines = _outside_fences(text)
    found: list[tuple[int, str]] = []
    prev_row = False
    for j, (no, line) in enumerate(lines):
        is_row = line.lstrip().startswith("|")
        if is_row and not prev_row:
            nxt = lines[j + 1][1].strip() if j + 1 < len(lines) else ""
            if not TABLE_DIVIDER.match(nxt):
                found.append((no, line))
        prev_row = is_row
    return found


def _excerpt(part: str, line_no: int) -> str:
    """The offending line with one line on each side. The neighbours are what
    show why it read that way -- a table row is only wrong given what follows."""
    lines = part.split("\n")
    start = max(0, line_no - 2)
    return "\n".join(lines[start : line_no + 1])


class SplitVerifier:
    def __init__(self, settings: RuntimeSettings) -> None:
        self._settings = settings

    def verify_chunks(self, text: str, chunks: list[str]) -> list[SplitProblem]:
        limit = self._settings.markdown_block_limit
        problems: list[SplitProblem] = []
        source = text.replace(SPLIT_MARKER, "")
        preexisting = {line.strip() for _, line in _header_less_rows(source)}
        kept = sum(len(c) for c in chunks)
        missing = len(source.strip()) - kept
        # Stripping markers/newlines naturally shrinks the text a bit; a
        # ratio-only check would false-positive on short answers, so this
        # also requires an absolute minimum gap.
        if missing > 200 and kept < len(source.strip()) * 0.97:
            # No line to point at: the whole chunk set lost content.
            problems.append(SplitProblem(f"내용 유실 의심 {len(source)} -> {kept}"))
        for i, part in enumerate(chunks):
            if len(part) > limit:
                problems.append(SplitProblem(f"{i}번 조각 상한 초과 {len(part)}"))
            # Only a line that opens or closes a fence counts. Counting every
            # occurrence made an inline code span carrying the three backticks
            # mid-sentence read as an unclosed fence (sca-a3b).
            fences = [no for no, x in enumerate(part.split("\n"), start=1) if FENCE_LINE.match(x)]
            if len(fences) % 2:
                problems.append(SplitProblem(
                    f"{i}번 조각 코드블록 펜스 짝 안 맞음", fences[-1], _excerpt(part, fences[-1])
                ))
            # A row the source already opened without a divider is the
            # answer's own formatting, not damage the split caused. Flagging
            # it sent an unsplit answer through safe_fallback, which cannot
            # restore the table and only costs the rest of the markup (sca-iyq).
            for no, line in _header_less_rows(part):
                if line.strip() in preexisting:
                    continue
                problems.append(SplitProblem(
                    f"{i}번 조각 표 열 이름 행 없음", no, _excerpt(part, no)
                ))
                break
            if SPLIT_MARKER in part:
                problems.append(SplitProblem(f"{i}번 조각 마커 잔존"))
        return problems

    def safe_fallback(self, text: str, limit: int | None = None) -> list[str]:
        # Last resort when verify_chunks() flags problems: split on line
        # boundaries only, sacrificing formatting to guarantee no content loss.
        if limit is None:
            limit = self._settings.markdown_block_limit
        parts: list[str] = []
        buf: list[str] = []
        for line in text.replace(SPLIT_MARKER, "").split("\n"):
            if buf and len("\n".join([*buf, line])) > limit:
                parts.append("\n".join(buf))
                buf = []
            buf.append(line)
        if buf:
            parts.append("\n".join(buf))
        parts = [piece for part in parts for piece in split_by_block_budget(part)]
        return parts or [text[:limit]]

    def separate_tables(self, text: str) -> str:
        # Slack folds a paragraph right after a table into the table itself
        # unless a blank line separates them, regardless of what other
        # separator (heading, bullet, quote) is used -- only a blank line
        # works. Prompting the model to avoid this isn't reliable enough,
        # so it's enforced here right before sending. Code fences are left
        # alone since they may contain a table-like example.
        lines = (text or "").split("\n")
        out = []
        in_fence = False
        prev_table = False
        for line in lines:
            if FENCE_LINE.match(line):
                in_fence = not in_fence
                out.append(line)
                prev_table = False
                continue
            if in_fence:
                out.append(line)
                continue

            is_table = bool(TABLE_LINE.match(line))
            blank = not line.strip()

            # Insert a blank line at a table/paragraph boundary in either direction.
            if (is_table and not prev_table and out and out[-1].strip()) or (
                prev_table and not is_table and not blank
            ):
                out.append("")

            out.append(line)
            prev_table = is_table
        return "\n".join(out)

    def block_limit_exceeded(self, exc: Exception) -> bool:
        """True when Slack refused the message for holding too many blocks.

        That cause is about length, not shape: the same content posts fine in
        smaller pieces. Other invalid_blocks causes do not, so they keep going
        down to plain text (sca-2k7).
        """
        if not self.blocks_rejected(exc):
            return False
        res = getattr(exc, "response", None)
        try:
            reasons = (res or {}).get("errors") or []
        except Exception:  # noqa: BLE001 -- unexpected response shape, fall back to the message text
            reasons = []
        text = " ".join([*(str(r) for r in reasons), str(exc)])
        return "items allowed" in text and "/blocks" in text

    def blocks_rejected(self, exc: Exception) -> bool:
        # Checks the response payload rather than the exception type, so
        # this keeps working across Slack SDK version upgrades.
        res = getattr(exc, "response", None)
        try:
            return (res or {}).get("error") == "invalid_blocks"
        except Exception:  # noqa: BLE001 -- fall back to string matching if response shape is unexpected
            return "invalid_blocks" in str(exc)
