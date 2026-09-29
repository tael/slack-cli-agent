# `SPLIT_MARKER` is a self-reported split boundary the model inserts into
# its own answer; `render/splitter.py` and `render/verifier.py` import it
# from here to stay in sync.

from __future__ import annotations

import re

SPLIT_MARKER = "<<<SPLIT>>>"


#: A table's divider row. Using it as a preview would notify "--- · ---".
_TABLE_DIVIDER_CELLS = re.compile(r"^\|?(\s*:?-+:?\s*\|)+\s*:?-*:?\s*\|?\s*$")


class BlockBuilder:
    # Only short trailing quotes get demoted to a context line -- some
    # answers legitimately end with a long quoted spec excerpt, and without
    # a length check that would get shrunk along with real footnotes.
    CONTEXT_MAX_LINES = 3
    CONTEXT_MAX_CHARS = 120

    def __init__(self, bot_display_name: str) -> None:
        self._bot_display_name = bot_display_name

    def clean_markers(self, text: str) -> str:
        # Must run before splitting into chunks -- a marker landing mid-table
        # or mid-fence would otherwise split it into a headerless fragment.
        lines = text.split("\n")
        out, in_fence = [], False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("```"):
                in_fence = not in_fence
                out.append(line)
                continue
            if stripped == SPLIT_MARKER:
                if in_fence:
                    continue
                prev = next((x.strip() for x in reversed(lines[:i]) if x.strip()), "")
                nxt = next((x.strip() for x in lines[i + 1:] if x.strip()), "")
                if prev.startswith("|") and nxt.startswith("|"):
                    continue
            out.append(line)
        return "\n".join(out)

    def preview(self, text: str) -> str:
        # When blocks are sent, `text` isn't rendered but still drives the
        # notification preview, so pull a plain first line for it -- strip
        # markdown emphasis since notifications don't render it.
        lines = [line.strip() for line in text.split("\n")]
        for line in lines:
            if not line or line.startswith(("#", "|", ">", "```", "---")):
                continue
            plain = self._plain(line)
            if plain:
                return plain[:150]
        # An answer made only of headings or only of a table used to notify as
        # "<봇> 답변", which says nothing about what arrived. Those lines are
        # skipped above because they render badly, not because they are empty.
        for line in lines:
            if line.startswith("#"):
                heading = self._plain(line.lstrip("# ").strip())
                if heading:
                    return heading[:150]
        for line in lines:
            if line.startswith("|") and not _TABLE_DIVIDER_CELLS.match(line):
                cells = [self._plain(c.strip()) for c in line.strip("|").split("|")]
                joined = " · ".join(c for c in cells if c)
                if joined:
                    return joined[:150]
        return self._bot_display_name + " 답변"

    @staticmethod
    def _plain(line: str) -> str:
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
        line = re.sub(r"[*_`~]", "", line)
        return line.lstrip("-+ ").strip()

    def split_context(self, text: str) -> tuple[str, str]:
        # Trailing metadata (elapsed time, timestamp, etc.) reads like part
        # of the answer at full size; demoting it to a Slack context block
        # (small gray text) sets it apart from the actual content.
        lines = text.rstrip().split("\n")
        note: list[str] = []
        while lines and lines[-1].lstrip().startswith(">"):
            note.insert(0, lines.pop().lstrip()[1:].strip())
            if len(note) > self.CONTEXT_MAX_LINES:
                return text, ""
        if not note:
            return text, ""
        joined = "\n".join(note)
        if len(joined) > self.CONTEXT_MAX_CHARS:
            return text, ""
        return "\n".join(lines).rstrip(), joined
