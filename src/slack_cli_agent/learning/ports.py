"""Protocols for the two data sources the learning batch reads.

Keeping these as protocols lets the batch be tested without a real filesystem
or Slack API behind them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol


class ResponseArchiveReader(Protocol):
    """Reads one day's response history per channel."""

    def read_day(self, day: str) -> Mapping[str, str]:
        ...


class ReactionSource(Protocol):
    """Collects human reactions to bot responses, per channel."""

    def collect(self, day: str) -> Mapping[str, Sequence[Mapping[str, object]]]:
        ...
