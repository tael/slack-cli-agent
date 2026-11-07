"""Keeps a resumed turn from re-sending instruction blocks the session already has.

codex and gemini fold this turn's instructions into the resumed prompt (sca-ivs),
so every resume pays the whole system prompt again -- channel knowledge alone is
65KB on private-taeil. The content has to stay current, so the prompt is still
composed in full each turn; only blocks whose bytes are identical to one the
session already received are swapped for a short reference to that block.

A block is what the composer writes under one top-level heading: the text
from a line starting with ``# `` (after a blank line) up to the next. A block changes as a
whole when a knowledge file, the asker or the run id changes, and then it is
sent again in full. Small blocks always go out as they are: a reference would
cost about as much and break up the text for nothing.

The record lives in this process only. After a restart nothing is known and
the first resumed turn sends everything again -- the cost comes back, never a
reference to text the session does not have.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections import OrderedDict

#: Blocks at or under this size are always sent in full.
MIN_REFERENCED_BLOCK_BYTES = 1024
#: Sessions remembered per engine. Oldest are forgotten first; a forgotten
#: session only loses the saving, its next resume sends everything.
MAX_SESSIONS = 4096

#: Top-level headings only. Splitting at every ``##`` cut the 24 knowledge
#: files on guchipachi into 279 blocks, most under the reference threshold, and
#: a resumed turn still carried 90897 of 646453 bytes; at ``# `` it was 5924
#: (2026-11-07 측정).
_BLOCK_START = re.compile(r"\n\n(?=# )")


def split_blocks(text: str) -> list[str]:
    """Splits at blank lines followed by a top-level heading. Joining the parts gives the
    original text back byte for byte."""
    if not text:
        return []
    parts: list[str] = []
    start = 0
    for match in _BLOCK_START.finditer(text):
        end = match.end()
        parts.append(text[start:end])
        start = end
    parts.append(text[start:])
    return [part for part in parts if part]


def block_id(block: str) -> str:
    return hashlib.sha256(block.encode("utf-8")).hexdigest()[:16]


def reference_for(block: str) -> str:
    """Names the block by its first line: the model never saw the id, only the
    text, so the heading is what lets it find the block it already has."""
    heading = block.lstrip("\n").split("\n", 1)[0].strip()[:80]
    return (
        f"[지침 블록 「{heading}」 ({block_id(block)}) : 이 대화에서 앞서 받은 같은 "
        f"블록과 내용이 같아 다시 싣지 않는다. 앞서 받은 그 블록을 그대로 따른다.]\n\n"
    )


class InstructionLedger:
    """Which instruction blocks each session has already received."""

    def __init__(self, max_sessions: int = MAX_SESSIONS) -> None:
        self._max_sessions = max_sessions
        self._sessions: OrderedDict[str, set[str]] = OrderedDict()
        self._lock = threading.Lock()

    def reduce(self, session_id: str | None, text: str) -> str:
        """The instruction text to send on a resumed turn of this session.

        Pure with respect to the ledger: the turn may still fail, so nothing
        is recorded here (see record)."""
        if not session_id or not text:
            return text
        with self._lock:
            known = set(self._sessions.get(session_id, ()))
        if not known:
            return text
        return "".join(
            reference_for(block)
            if len(block.encode("utf-8")) > MIN_REFERENCED_BLOCK_BYTES and block_id(block) in known
            else block
            for block in split_blocks(text)
        )

    def record(self, session_id: str | None, text: str) -> None:
        """Called once a turn carrying ``text`` in full finished successfully."""
        if not session_id or not text:
            return
        ids = {block_id(block) for block in split_blocks(text)}
        with self._lock:
            known = self._sessions.pop(session_id, set())
            known |= ids
            self._sessions[session_id] = known
            while len(self._sessions) > self._max_sessions:
                self._sessions.popitem(last=False)
