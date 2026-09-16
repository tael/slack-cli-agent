"""Manages status reaction emoji.

    eyes              being handled now
    hourglass         queued behind an earlier request on the same thread
    white_check_mark  answered
    x                 failed
    zipper_mouth_face chose not to answer
    mag               handed off to a watch queue (not done)

eyes/hourglass/x survive a process crash, since none of them mean
"done" — recovery still treats them as unfinished. A human manually
adding white_check_mark counts the same way, as a manual override.

Reaction API failures don't propagate: one failed reaction shouldn't halt
the rest of processing. They are logged at debug level — swallowing them
without a trace left no way to tell a failed call from one that was never
made (sca-aj3).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from slack_cli_agent.guard.watch import WATCH_MARK_EMOJI

log = logging.getLogger(__name__)

SILENT_MARK_EMOJI = "zipper_mouth_face"
DONE_EMOJI = frozenset({"white_check_mark", SILENT_MARK_EMOJI})
UNFINISHED_EMOJI = frozenset({"eyes", "hourglass", "x"})

# A human reacting with this emoji triggers a postmortem review of that reply.
POSTMORTEM_EMOJI = "dango"
DEBUG_TRACE_EMOJI = "brain"
FORMAT_REVIEW_EMOJI = "pencil2"


class ReactionMarker:
    def __init__(self, client: Any) -> None:
        self._client = client

    def add(self, channel: str, ts: str, name: str) -> None:
        try:
            self._client.reactions_add(channel=channel, timestamp=ts, name=name)
        except Exception as exc:  # noqa: BLE001 - reactions are cosmetic; don't propagate per the module docstring
            log.debug("이모지 추가 실패 : %s %s:%s, %s", name, channel, ts, exc)

    def remove(self, channel: str, ts: str, name: str) -> None:
        try:
            self._client.reactions_remove(channel=channel, timestamp=ts, name=name)
        except Exception as exc:  # noqa: BLE001 - reactions are cosmetic; don't propagate per the module docstring
            log.debug("이모지 제거 실패 : %s %s:%s, %s", name, channel, ts, exc)

    def mark_processing(self, channel: str, ts: str) -> None:
        self.add(channel, ts, "eyes")

    def clear_processing(self, channel: str, ts: str) -> None:
        self.remove(channel, ts, "eyes")

    def mark_waiting(self, channel: str, ts: str) -> None:
        """Only for a request that can't start yet. eyes and hourglass are
        mutually exclusive — adding this unconditionally put both on every
        request, which left hourglass meaning nothing."""
        self.add(channel, ts, "hourglass")

    def clear_waiting(self, channel: str, ts: str) -> None:
        """The wait is over — the request is being handled now."""
        self.remove(channel, ts, "hourglass")

    def _settle(self, channel: str, ts: str, mark: str) -> None:
        """Clears unfinished marks and applies the final one.

        Clears hourglass as well as eyes: a request that finished while
        still carrying the queued mark would look pending forever if the
        worker never got to clear it. Doesn't clear the mark being
        applied itself — x doubles as an unfinished mark, and clearing
        then re-adding it would cost an extra Slack call for nothing.
        """
        for stale in UNFINISHED_EMOJI:
            if stale != mark:
                self.remove(channel, ts, stale)
        self.add(channel, ts, mark)

    def mark_done(self, channel: str, ts: str) -> None:
        self._settle(channel, ts, "white_check_mark")

    def mark_failed(self, channel: str, ts: str) -> None:
        self._settle(channel, ts, "x")

    def mark_silent(self, channel: str, ts: str) -> None:
        """Not answering is still a resolution — leave a trace of it."""
        self._settle(channel, ts, SILENT_MARK_EMOJI)

    def mark_watch(self, channel: str, ts: str) -> None:
        """Handed off to a watch queue isn't done — white_check_mark
        would exclude it from recovery's unfinished scan."""
        self._settle(channel, ts, WATCH_MARK_EMOJI)

    def mark_resolved_like(self, channel: str, ts: str, mark: str) -> None:
        """Applies the same resolution mark as another related
        request, clearing any unfinished mark first."""
        self._settle(channel, ts, mark)

    def reactions_on(self, msg: Mapping[str, Any]) -> set[str]:
        return {r.get("name") for r in (msg.get("reactions") or []) if r.get("name")}

    def already_handled(self, msg: Mapping[str, Any]) -> bool:
        """Whether this message has already been judged (white_check_mark or zipper_mouth_face)."""
        return bool(self.reactions_on(msg) & DONE_EMOJI)
