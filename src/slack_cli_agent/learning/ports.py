"""Protocols for the two data sources the learning batch reads.

Keeping these as protocols lets the batch be tested without a real filesystem
or Slack API behind them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class DayArchives:
    """One day's history, with the channels that could not be read named.

    Skipping an unreadable channel makes it indistinguishable from one with no
    history, and callers act on that difference: dropping a channel's learning
    progress on that basis loses its retry for good (sca-b4o review). Both come
    from a single scan so the two never disagree.
    """

    texts: Mapping[str, str] = field(default_factory=dict)
    unreadable: frozenset[str] = frozenset()


class ResponseArchiveReader(Protocol):
    """Reads one day's response history per channel."""

    def read_day(self, day: str) -> DayArchives:
        ...


class ReactionSource(Protocol):
    """Collects human reactions to bot responses, per channel."""

    def collect(self, texts: Mapping[str, str]) -> Mapping[str, Sequence[Mapping[str, object]]]:
        ...
