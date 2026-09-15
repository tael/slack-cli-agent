"""Decodes one learning proposal out of an engine's free-form reply.

Each engine's parse() normalizes the CLI's own output format, but what the
model wrote arrives verbatim in `response.body`. Whether it comes fenced, with
a sentence before it, or with the schema from the prompt repeated back differs
per engine, so that difference is absorbed here rather than in each engine
(sca-vpv). Keeping it in the learning domain also means a new engine needs an
adapter, not a second copy of these rules.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..core.result import Outcome

#: Fields the proposal carries as a list of sentences.
_LIST_FIELDS = ("writing_style", "channel_facts", "corrections")

#: Every field the schema defines. An object carrying none of them is some
#: other JSON in the reply, not a proposal that happens to be empty.
_KNOWN_FIELDS = (*_LIST_FIELDS, "note")

#: How many `{` positions decode() will try before giving up. Each failed try
#: re-reads the rest of the body, so a reply full of unclosed braces costs
#: O(N^2) (codex review). A length cap would also reject a long but valid
#: reply; this only rejects one that is mostly unparseable.
_MAX_SCAN_ATTEMPTS = 500


@dataclass(frozen=True)
class ChannelAnalysisResult:
    """Result of analyzing a single channel."""

    writing_style: tuple[str, ...] = ()
    channel_facts: tuple[str, ...] = ()
    corrections: tuple[str, ...] = ()
    note: str = ""


class ProposalDecoder:
    """Finds the proposal object in a reply and checks its shape.

    Shape checking is not optional politeness: `tuple()` over a string splits
    it into characters and over a mapping keeps only the keys, so a wrong type
    would become a plausible-looking proposal rather than a failure.
    """

    def decode(self, body: str) -> Outcome[ChannelAnalysisResult]:
        objects, exhausted = _json_objects(body or "")
        candidates = [c for c in objects if self._shape_ok(c)]
        if exhausted and not candidates:
            return Outcome.unknown(f"JSON 으로 안 읽히는 조각이 {_MAX_SCAN_ATTEMPTS}개를 넘었다")
        if not candidates:
            return Outcome.unknown("제안 형식이 맞지 않다")
        if len(candidates) > 1:
            # Picking one would hide which of them the day's proposal came from.
            return Outcome.unknown(f"제안으로 읽히는 JSON 이 {len(candidates)}개다")
        return Outcome.found(self._build(candidates[0]))

    def _shape_ok(self, data: Mapping[str, object]) -> bool:
        if not any(name in data for name in _KNOWN_FIELDS):
            return False
        for name in _LIST_FIELDS:
            value = data.get(name)
            if value is None:
                # An omitted empty list is not a format violation.
                continue
            if isinstance(value, str) or not isinstance(value, Sequence):
                return False
            if any(not isinstance(item, str) for item in value):
                return False
        note = data.get("note")
        return note is None or isinstance(note, str)

    def _build(self, data: Mapping[str, object]) -> ChannelAnalysisResult:
        return ChannelAnalysisResult(
            writing_style=_strings(data.get("writing_style")),
            channel_facts=_strings(data.get("channel_facts")),
            corrections=_strings(data.get("corrections")),
            note=str(data.get("note") or ""),
        )


def _strings(value: object) -> tuple[str, ...]:
    """Only reached after _shape_ok(); anything else is dropped rather than
    coerced, since a coercion here would be the silent rewrite it guards against.
    """
    if not isinstance(value, Sequence) or isinstance(value, str):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _json_objects(text: str) -> tuple[list[Mapping[str, object]], bool]:
    """Every top-level JSON object in the text, and whether the scan gave up.

    Scans for balanced braces rather than matching a regex: a greedy brace-to-brace regex
    swallows everything between the first and last brace, so one sentence
    around the proposal was enough to break it.
    """
    found: list[Mapping[str, object]] = []
    decoder = json.JSONDecoder()
    index = 0
    attempts = 0
    while True:
        start = text.find("{", index)
        if start < 0:
            return found, False
        attempts += 1
        if attempts > _MAX_SCAN_ATTEMPTS:
            return found, True
        try:
            value, end = decoder.raw_decode(text, start)
        except ValueError:
            # Not the start of a valid object; the next brace may still be.
            index = start + 1
            continue
        if isinstance(value, Mapping):
            found.append(value)
        index = end
