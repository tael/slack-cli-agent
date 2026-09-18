"""Validates split chunks before sending and provides a safe fallback."""

from __future__ import annotations

import re

from slack_cli_agent.config.settings import RuntimeSettings

from .blocks import SPLIT_MARKER
from .splitter import split_by_block_budget

TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")
#: A GFM table separator row. The pipe may be followed by spaces and the
#: alignment colons are optional, so "|-" as a prefix test rejects the form
#: models actually write -- "| --- | --- |" (sca-3pr).
TABLE_DIVIDER = re.compile(r"^\|?(\s*:?-+:?\s*\|)+\s*:?-*:?\s*\|?\s*$")
FENCE_LINE = re.compile(r"^\s*```")


class SplitVerifier:
    def __init__(self, settings: RuntimeSettings) -> None:
        self._settings = settings

    def verify_chunks(self, text: str, chunks: list[str]) -> list[str]:
        limit = self._settings.markdown_block_limit
        problems = []
        source = text.replace(SPLIT_MARKER, "")
        kept = sum(len(c) for c in chunks)
        missing = len(source.strip()) - kept
        # Stripping markers/newlines naturally shrinks the text a bit; a
        # ratio-only check would false-positive on short answers, so this
        # also requires an absolute minimum gap.
        if missing > 200 and kept < len(source.strip()) * 0.97:
            problems.append(f"내용 유실 의심 {len(source)} -> {kept}")
        for i, part in enumerate(chunks):
            if len(part) > limit:
                problems.append(f"{i}번 조각 상한 초과 {len(part)}")
            if part.count("```") % 2:
                problems.append(f"{i}번 조각 코드블록 펜스 짝 안 맞음")
            # Checking only the chunk's start would miss tables that begin
            # mid-chunk, so scan every place a table row starts for the
            # header divider that must follow it.
            lines = [x for x in part.split("\n") if x.strip()]
            prev_row = False
            for j, line in enumerate(lines):
                is_row = line.lstrip().startswith("|")
                if is_row and not prev_row:
                    nxt = lines[j + 1].strip() if j + 1 < len(lines) else ""
                    if not TABLE_DIVIDER.match(nxt):
                        problems.append(f"{i}번 조각 표 열 이름 행 없음")
                        break
                prev_row = is_row
            if SPLIT_MARKER in part:
                problems.append(f"{i}번 조각 마커 잔존")
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

    def blocks_rejected(self, exc: Exception) -> bool:
        # Checks the response payload rather than the exception type, so
        # this keeps working across Slack SDK version upgrades.
        res = getattr(exc, "response", None)
        try:
            return (res or {}).get("error") == "invalid_blocks"
        except Exception:  # noqa: BLE001 -- fall back to string matching if response shape is unexpected
            return "invalid_blocks" in str(exc)
