"""Splits message text to fit Slack's length limits."""

from __future__ import annotations

import re

from slack_cli_agent.config.settings import RuntimeSettings

from .blocks import SPLIT_MARKER, BlockBuilder

# Tables, code blocks, and quotes render broken if split mid-block, even by one line.
ATOMIC_HEADS = ("|", "```", ">")


class ContentSplitter:
    def __init__(self, settings: RuntimeSettings, block_builder: BlockBuilder) -> None:
        self._settings = settings
        self._blocks = block_builder

    def chunk(self, text: str, size: int | None = None) -> list[str]:
        if size is None:
            size = self._settings.slack_chunk
        if len(text) <= size:
            return [text]
        parts, buf = [], ""
        for line in text.split("\n"):
            if len(buf) + len(line) + 1 > size:
                if buf:
                    parts.append(buf)
                    buf = ""
                while len(line) > size:
                    parts.append(line[:size])
                    line = line[size:]
            buf = f"{buf}\n{line}" if buf else line
        if buf:
            parts.append(buf)
        return parts

    def line_kind(self, line: str) -> str:
        stripped = line.strip()
        if stripped.startswith("|"):
            return "table"
        if stripped.startswith(">"):
            return "quote"
        if re.match(r"[-*+]\s|\d+[.)]\s", stripped):
            return "list"
        if re.match(r"\s+", line) and stripped:
            return "list"  # indented lines continue the preceding list item
        if not stripped:
            return "blank"
        return "text"

    def md_chunks(self, text: str) -> list[str]:
        # Split by line, not paragraph, so blocks with no blank-line separators still get boundaries.
        chunks: list[str] = []
        buf: list[str] = []
        in_fence, mode = False, None

        def flush() -> None:
            if buf:
                chunks.append("\n".join(buf))
                buf.clear()

        for line in text.split("\n"):
            stripped = line.strip()

            if in_fence:
                buf.append(line)
                if stripped.startswith("```"):
                    flush()
                    in_fence = False
                    mode = None
                continue
            if stripped.startswith("```"):
                flush()
                buf.append(line)
                in_fence = True
                continue

            kind = self.line_kind(line)
            if kind == "blank":
                # Blank lines don't close a block since they may just be spacing between list items.
                buf.append(line)
                continue
            if mode and kind != mode:
                flush()
            if not mode or kind != mode:
                mode = kind if kind in ("table", "quote", "list") else None
                if mode is None and buf and self.line_kind(buf[-1]) != "text":
                    pass
            buf.append(line)
        flush()
        return [c for c in chunks if c.strip() or c == ""]

    def fit_chunk(self, chunk: str, limit: int) -> list[str]:
        # Tables get their header + separator row repeated in each piece, or later
        # pieces render as literal pipe characters instead of a table.
        if len(chunk) <= limit:
            return [chunk]
        lines = chunk.split("\n")

        if lines[0].strip().startswith("|") and len(lines) > 2:
            head = lines[:2]
            out, cur = [], list(head)
            for row in lines[2:]:
                if len(cur) > 2 and len("\n".join([*cur, row])) > limit:
                    out.append("\n".join(cur))
                    cur = list(head)
                cur.append(row)
            if len(cur) > 2:
                out.append("\n".join(cur))
            return out

        if lines[0].strip().startswith("```"):
            fence = lines[0]
            body = lines[1:-1] if lines[-1].strip().startswith("```") else lines[1:]
            out, cur = [], [fence]
            for row in body:
                if len(cur) > 1 and len("\n".join([*cur, row, "```"])) > limit:
                    cur.append("```")
                    out.append("\n".join(cur))
                    cur = [fence]
                cur.append(row)
            cur.append("```")
            out.append("\n".join(cur))
            return out

        if lines[0].strip().startswith(">"):
            out, cur = [], []
            for row in lines:
                if cur and len("\n".join([*cur, row])) > limit:
                    out.append("\n".join(cur))
                    cur = []
                cur.append(row)
            if cur:
                out.append("\n".join(cur))
            return out

        if len(lines) > 1:
            out, cur = [], []
            for row in lines:
                if cur and len("\n".join([*cur, row])) > limit:
                    out.append("\n".join(cur))
                    cur = []
                cur.append(row)
            if cur:
                out.append("\n".join(cur))
            return out

        return [chunk[i:i + limit] for i in range(0, len(chunk), limit)]

    def split_for_blocks(self, text: str, limit: int | None = None) -> list[str]:
        # Prefer split markers the source text already placed (it knows the intended
        # boundaries), but ignore any that land inside a table or code block, since
        # splitting there produces a headerless fragment that renders as raw pipes.
        if limit is None:
            limit = self._settings.markdown_block_limit
        text = self._blocks.clean_markers(text)
        parts: list[str] = []
        forced: list[bool] = []
        buf: list[str] = []

        def flush(by_marker: bool = False, final: bool = False) -> None:
            if not buf:
                return
            if final:
                # No later chunk to push a trailing heading into, so keep it as-is.
                parts.append("\n".join(buf).strip("\n"))
                forced.append(by_marker)
                buf.clear()
                return
            # A chunk ending in a bare heading reads as a title with no content,
            # so carry the heading over to the next chunk along with what follows it.
            trailing: list[str] = []
            while buf and (not buf[-1].strip() or buf[-1].strip().startswith("#")):
                trailing.insert(0, buf.pop())
            if not buf:
                buf.extend(trailing)
                trailing = []
            parts.append("\n".join(buf).strip("\n"))
            forced.append(by_marker)
            buf.clear()
            buf.extend(x for x in trailing if x.strip())

        room = int(limit * 0.92)  # leave headroom so adjoining text has room to attach
        for block in self.md_chunks(text):
            if block.lstrip().startswith(("|", "```")):
                segments = [block.replace(SPLIT_MARKER, "").rstrip()]
            else:
                segments = block.split(SPLIT_MARKER)
            for idx, seg in enumerate(segments):
                if idx:
                    flush(by_marker=True)
                seg = seg.strip("\n")
                if not seg.strip():
                    continue
                for piece in self.fit_chunk(seg, room):
                    if buf and len("\n".join([*buf, piece])) > limit:
                        flush()
                    buf.append(piece)
        flush(final=True)
        return self.merge_tiny(parts, forced, limit)

    def merge_tiny(
        self, parts: list[str], forced: list[bool], limit: int, floor: int = 500
    ) -> list[str]:
        # Merges small leftover fragments (e.g. the last few rows of a split table)
        # into the previous chunk, but leaves explicit marker splits alone.
        out: list[str] = []
        for part, by_marker in zip(parts, forced, strict=True):
            joinable = out and not by_marker and (len(part) < floor or len(out[-1]) < floor)
            if joinable and len(out[-1]) + len(part) + 1 <= limit:
                out[-1] = out[-1] + "\n" + part
            else:
                out.append(part)
        return [p for p in out if p.strip()]
