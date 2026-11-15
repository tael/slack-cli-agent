"""Decides whether a channel's day is worth an LLM analysis, and carries over
the days that were not.

A day skipped for low volume is not "finished" for that channel: its record is
kept on a carry list and analyzed together with the next analyzed day, so a
skip delays learning but never drops it. Three triggers force an analysis:
a correction signal in today's questions, the accumulated volume reaching the
threshold, and the oldest carried day reaching the maximum age.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

_DAY_FORMAT = "%Y-%m-%d"
# One archive entry per handled request; see ResponseArchive.record().
_ENTRY_PATTERN = re.compile(r"^## \d\d:\d\d KST", re.MULTILINE)
_QUESTION_PATTERN = re.compile(r"### 질문\n\n(.*?)\n\n### 응답", re.DOTALL)
# Phrases a person uses when the bot was wrong or told to stop doing
# something. Kept narrow on purpose: "다시" and "왜" are in ordinary requests
# and would make every day a signal day.
_CORRECTION_PATTERN = re.compile(
    r"아니[다지에야요]|아냐|그[게거] 아니|틀[렸림려]|잘못|하지 ?(?:마|말)|말고"
    r"|군더더기|쓸모없|재발 ?방지|했어야"
)


def count_entries(text: str) -> int:
    """Number of requests recorded in one channel-day archive."""
    return len(_ENTRY_PATTERN.findall(text))


def has_correction_signal(text: str) -> bool:
    """Whether any question in the archive reads as a correction.

    Only the question side is searched: the bot's own answers quote such
    phrases when explaining themselves.
    """
    return any(_CORRECTION_PATTERN.search(q) for q in _QUESTION_PATTERN.findall(text))


@dataclass(frozen=True)
class GatePolicy:
    #: Accumulated entries (carried days plus today) that justify an analysis.
    #: 1 or less analyzes every channel-day, i.e. the gate is off.
    min_entries: int
    #: Days the oldest carried day may wait before an analysis is forced.
    max_defer_days: int

    def __post_init__(self) -> None:
        if self.max_defer_days < 1:
            raise ValueError(f"최대 이월 일수는 1 이상이어야 한다 : {self.max_defer_days}")


@dataclass(frozen=True)
class GateDecision:
    analyze: bool
    reason: str


class LearningGate:
    def __init__(self, policy: GatePolicy) -> None:
        self._policy = policy

    def decide(
        self, *, day: str, carried_days: Sequence[str], entries: int, signal: bool,
    ) -> GateDecision:
        if signal:
            return GateDecision(True, "정정 신호")
        if carried_days and self.age_days(day, carried_days) >= self._policy.max_defer_days:
            return GateDecision(
                True, f"이월 {self.age_days(day, carried_days)}일 경과(상한 {self._policy.max_defer_days}일)",
            )
        if entries >= self._policy.min_entries:
            return GateDecision(True, f"누적 {entries}건(기준 {self._policy.min_entries}건)")
        return GateDecision(
            False, f"누적 {entries}건 < 기준 {self._policy.min_entries}건, 정정 신호 없음",
        )

    def due_channels(self, day: str, carry: Mapping[str, Sequence[str]]) -> set[str]:
        """Channels whose carried record hit the age limit.

        They are analyzed even on a day with no archive of their own, or a
        channel that went quiet after a skip would never be read.
        """
        due = set()
        for channel, days in carry.items():
            earlier = [d for d in days if d < day]
            if earlier and self.age_days(day, earlier) >= self._policy.max_defer_days:
                due.add(channel)
        return due

    @staticmethod
    def age_days(day: str, carried_days: Sequence[str]) -> int:
        return (date.fromisoformat(day) - date.fromisoformat(min(carried_days))).days


class CarryStore:
    """Channel to the days skipped without analysis, in one JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> dict[str, tuple[str, ...]]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError("최상위가 사전이 아니다")
            return {
                str(channel): tuple(sorted(str(d) for d in days)) for channel, days in raw.items()
            }
        except (OSError, ValueError, TypeError) as exc:
            # An unreadable list cannot be rebuilt, but stopping every tick on
            # it would stop learning altogether. The file is kept for repair.
            quarantine = self._path.with_name(self._path.name + ".corrupt")
            log.warning("이월 목록을 읽지 못했다. %s 로 옮기고 비어 있는 것으로 시작한다 : %s", quarantine, exc)
            try:
                os.replace(self._path, quarantine)
            except OSError:
                pass
            return {}

    def add(self, channel: str, day: str) -> None:
        carry = self.load()
        carry[channel] = tuple(sorted({*carry.get(channel, ()), day}))
        self._write(carry)

    def clear(self, channel: str, days: Sequence[str]) -> None:
        carry = self.load()
        left = tuple(d for d in carry.get(channel, ()) if d not in set(days))
        if left:
            carry[channel] = left
        else:
            carry.pop(channel, None)
        self._write(carry)

    def _write(self, carry: Mapping[str, Sequence[str]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps({k: list(v) for k, v in carry.items()}, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)
