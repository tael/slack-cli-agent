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

    #: Prefix for a paragraph that follows a list in its own markdown block.
    #: A separate rich_text block still renders flush under the last bullet,
    #: and Slack drops a leading newline. A zero-width space line survives
    #: conversion and renders as one blank line. 2026-11-10 #test-agent
    #: screenshots: split alone and a leading newline stayed glued, this did not.
    LIST_GAP = "\u200b\n"

    #: A list item line: "- x", "* x", "+ x", "1. x", "1) x", any indent.
    _LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S")

    def split_after_lists(self, text: str) -> list[str]:
        """Cuts the body where a plain paragraph follows a list.

        Slack converts one markdown block into one rich_text block. A
        paragraph right after a list lands in that same rich_text block as a
        section whose leading newline the client drops, so it renders glued
        under the last (often nested) bullet. 2026-11-10 private-taeil : the
        closing bold question stuck to a sub-bullet even with a blank line
        between them. Giving the paragraph its own markdown block makes Slack
        store a separate rich_text block, which renders at block level.

        A line starts a new part when it sits at column 0, is not a list item,
        heading, quote, table row or fence, and the last non-blank line before
        it belongs to a list (item or indented continuation). Fenced code is
        never cut.
        """
        lines = text.split("\n")
        parts: list[list[str]] = [[]]
        in_fence = False
        in_list = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("```"):
                in_fence = not in_fence
                in_list = False
                parts[-1].append(line)
                continue
            if in_fence or not stripped:
                parts[-1].append(line)
                continue
            if self._LIST_ITEM.match(line):
                in_list = True
            elif line[0] in " \t" and in_list:
                pass  # indented continuation of the current item
            else:
                starts_block = stripped[0] in "#>|"
                if in_list and not starts_block and any(x.strip() for x in parts[-1]):
                    parts.append([])
                in_list = False
            parts[-1].append(line)
        out = ["\n".join(p).strip("\n") for p in parts]
        return [p for p in out if p.strip()] or [text]
