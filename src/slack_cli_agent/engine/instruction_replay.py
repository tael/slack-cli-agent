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
from dataclasses import dataclass, field

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


def heading_of(block: str) -> str:
    """The block's first line: the only name the model can match a reference to."""
    return block.lstrip("\n").split("\n", 1)[0].strip()[:80]


def reference_for(block: str) -> str:
    """Names the block by its first line: the model never saw the id, only the
    text, so the heading is what lets it find the block it already has."""
    return (
        f"[지침 블록 「{heading_of(block)}」 ({block_id(block)}) : 이 대화에서 앞서 받은 같은 "
        f"블록과 내용이 같아 다시 싣지 않는다. 앞서 받은 그 블록을 그대로 따른다.]\n\n"
    )


@dataclass
class _Session:
    #: Blocks that reached the session in full and stay there.
    known: set[str] = field(default_factory=set)
    #: heading -> id of the version the session saw last under it, None when
    #: one text carried two versions. A heading is the only key the model has,
    #: so a block is referenced only while it is the latest version under its
    #: heading. Prompt library sections have no ``# `` line, so the owner and
    #: non-owner notes ride in whatever block precedes them and that block
    #: changes with the asker within one thread.
    latest: dict[str, str | None] = field(default_factory=dict)
    #: Resumed turns sent with references since the last full send.
    referenced_turns: int = 0


def _versions(text: str) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for block in split_blocks(text):
        heading = heading_of(block)
        out[heading] = block_id(block) if heading not in out else None
    return out


class InstructionLedger:
    """Which instruction blocks each session has already received."""

    def __init__(
        self, max_sessions: int = MAX_SESSIONS, refresh_after: int | None = None
    ) -> None:
        self._max_sessions = max_sessions
        #: After this many referenced turns in a row the next one goes out in
        #: full. None never refreshes.
        self._refresh_after = refresh_after
        self._sessions: OrderedDict[str, _Session] = OrderedDict()
        self._lock = threading.Lock()

    def reduce(self, session_id: str | None, text: str) -> str:
        """The instruction text to send on a resumed turn of this session.

        Pure with respect to the ledger: the turn may still fail, so nothing
        is recorded here (see record)."""
        if not session_id or not text:
            return text
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None or not state.known:
                return text
            if self._refresh_after is not None and state.referenced_turns >= self._refresh_after:
                return text
            known = set(state.known)
            latest = dict(state.latest)
        here = _versions(text)
        out = []
        for block in split_blocks(text):
            bid = block_id(block)
            heading = heading_of(block)
            reusable = (
                len(block.encode("utf-8")) > MIN_REFERENCED_BLOCK_BYTES
                and bid in known
                and latest.get(heading) == bid
                and here.get(heading) == bid
            )
            out.append(reference_for(block) if reusable else block)
        return "".join(out)

    def record(
        self,
        session_id: str | None,
        text: str,
        *,
        delivered: bool = True,
        referenced: bool = False,
    ) -> None:
        """Called after a turn carrying ``text`` went out.

        ``delivered`` says its blocks now stay in the session; a failed turn
        or one whose prompt may be compacted only moves the latest version per
        heading, which can stop a reference but never start one.
        ``referenced`` says the turn went out reduced."""
        if not session_id or not text:
            return
        versions = _versions(text)
        with self._lock:
            state = self._sessions.pop(session_id, None) or _Session()
            if delivered:
                state.known |= {block_id(block) for block in split_blocks(text)}
            state.latest.update(versions)
            if referenced:
                state.referenced_turns += 1
            elif delivered:
                state.referenced_turns = 0
            self._sessions[session_id] = state
            while len(self._sessions) > self._max_sessions:
                self._sessions.popitem(last=False)
