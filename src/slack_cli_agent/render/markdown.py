# Converts common markdown habits into Slack mrkdwn, since Slack doesn't
# render tables and shows double-asterisk emphasis literally.

from __future__ import annotations

import re


class MarkdownConverter:
    def tables_to_bullets(self, text: str) -> str:
        lines = text.split("\n")
        out = []
        i = 0
        while i < len(lines):
            line = lines[i]
            is_row = line.strip().startswith("|") and line.strip().endswith("|")
            divider = (
                i + 1 < len(lines)
                and re.fullmatch(r"\s*\|[\s:|-]+\|\s*", lines[i + 1] or "")
            )
            if not (is_row and divider):
                out.append(line)
                i += 1
                continue

            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                cells = [c for c in cells if c]
                if cells:
                    out.append("- " + " ".join(cells))
                i += 1
        return "\n".join(out)

    def to_mrkdwn(self, text: str) -> str:
        parts = text.split("```")
        for i in range(0, len(parts), 2):   # even indices are outside code fences
            b = parts[i]
            b = re.sub(r"\*\*\*(.+?)\*\*\*", r"*\1*", b)
            b = re.sub(r"\*\*(.+?)\*\*", r"*\1*", b)
            b = re.sub(r"__(.+?)__", r"_\1_", b)
            b = re.sub(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", r"*\1*", b, flags=re.MULTILINE)
            b = re.sub(r"^\s*[•◦▪]\s+", "- ", b, flags=re.MULTILINE)
            b = re.sub(r"^\s*[-*]{3,}\s*$", "", b, flags=re.MULTILINE)
            # Tables must be converted before links, or a link's pipe-adjacent
            # brackets get mistaken for cell separators.
            b = self.tables_to_bullets(b)
            b = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", b)
            parts[i] = b
        return "```".join(parts)
