"""Per-channel progress for one day's learning batch.

Marking a day done regardless of per-channel failures lost that channel's
learning permanently; retrying the whole day instead re-analyzed the channels
that already succeeded, every tick (sca-b4o). Progress is therefore tracked per
channel: a later tick only re-analyzes what is still outstanding, and the day's
proposal is rebuilt from stored results plus the new ones.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

from .decoder import ChannelAnalysisResult

# How many times a plain analysis failure is retried before the day gives up on
# that channel. Without a ceiling a permanently broken channel keeps the day
# incomplete, and schedule.py keeps offering it every tick.
MAX_ATTEMPTS = 3


class FailureKind(StrEnum):
    USAGE_LIMIT = "usage_limit"
    AUTH_FAILURE = "auth_failure"
    ENGINE_FAILED = "engine_failed"
    DECODE_FAILED = "decode_failed"
    ARCHIVE_UNREADABLE = "archive_unreadable"

    @property
    def description(self) -> str:
        return _DESCRIPTIONS[self]

    @property
    def waits_for_approval(self) -> bool:
        """Whether this clears by itself once someone acts, rather than by retrying.

        인증 실패도 사람이 다시 로그인하거나 엔진 전환을 승인해야 풀린다.
        재시도 횟수를 깎으면 그 채널이 사람 손이 닿기 전에 포기된다.
        """
        return self in (FailureKind.USAGE_LIMIT, FailureKind.AUTH_FAILURE)

    @property
    def retry_delay(self) -> timedelta:
        """How long to leave this channel alone before trying it again.

        A usage limit is never given up on, so without a wait the batch would
        call the engine every tick until someone approves the switch.
        """
        return _RETRY_DELAYS[self]


_RETRY_DELAYS: Mapping[FailureKind, timedelta] = {
    FailureKind.USAGE_LIMIT: timedelta(minutes=30),
    FailureKind.AUTH_FAILURE: timedelta(minutes=30),
    FailureKind.ENGINE_FAILED: timedelta(minutes=10),
    FailureKind.DECODE_FAILED: timedelta(minutes=10),
    FailureKind.ARCHIVE_UNREADABLE: timedelta(minutes=10),
}

_DESCRIPTIONS: Mapping[FailureKind, str] = {
    FailureKind.USAGE_LIMIT: "사용 한도에 걸렸습니다. 엔진 전환을 승인하시면 다시 시도합니다.",
    FailureKind.AUTH_FAILURE: "실행기 로그인이 풀렸습니다. 다시 로그인하거나 엔진 전환을 승인하시면 다시 시도합니다.",
    FailureKind.ENGINE_FAILED: "분석 실행이 실패했습니다.",
    FailureKind.DECODE_FAILED: "분석 결과를 읽지 못했습니다.",
    FailureKind.ARCHIVE_UNREADABLE: "대화 기록 파일을 읽지 못했습니다.",
}


@dataclass(frozen=True)
class ChannelFailure:
    channel: str
    kind: FailureKind
    detail: str
    #: Counts consecutive plain failures only. A usage limit is exempt from the
    #: ceiling, so letting it increment this would make one limit hit eat into
    #: the retries a plain failure is entitled to (sca-b4o review).
    attempts: int = 1
    #: None means it may be tried on the next tick.
    next_retry_at: datetime | None = None

    def due(self, now: datetime) -> bool:
        return self.next_retry_at is None or self.next_retry_at <= now

    @property
    def retryable(self) -> bool:
        # A usage limit clears when the owner approves the switch, so giving up
        # on it would make that approval do nothing.
        return self.kind.waits_for_approval or self.attempts < MAX_ATTEMPTS

    @property
    def key(self) -> str:
        """What "the same failure" means for notification purposes."""
        return f"{self.channel}:{self.kind}"

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": str(self.kind), "detail": self.detail,
            "attempts": self.attempts,
            "next_retry_at": self.next_retry_at.isoformat() if self.next_retry_at else "",
        }

    @classmethod
    def from_dict(cls, channel: str, data: Mapping[str, object]) -> ChannelFailure:
        # Every key is required and every value is validated here. Repairing a
        # broken field in place would leave the day looking finished, and the
        # policy for an unreadable file is to re-analyze it (sca-b4o review).
        return cls(
            channel=channel,
            kind=FailureKind(_as_str(data["kind"])),
            detail=_as_str(data["detail"]),
            attempts=_as_attempts(data["attempts"]),
            next_retry_at=_parse_retry_at(_as_str(data["next_retry_at"])),
        )


def _as_str(value: object) -> str:
    # str() would accept anything, which makes the readable=False policy vacuous
    # for every scalar in the file (sca-b4o review).
    if not isinstance(value, str):
        raise TypeError(f"문자열이 아닙니다 : {value!r}")
    return value


def _as_attempts(value: object) -> int:
    # bool is an int subclass, and a negative count would keep a plain failure
    # from ever reaching the retry ceiling.
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TypeError(f"시도 횟수가 아닙니다 : {value!r}")
    return value


def _parse_retry_at(value: str) -> datetime | None:
    """Raises rather than deferring the parse to due(), which would stop a tick."""
    if not value:
        return None
    when = datetime.fromisoformat(value)
    if when.tzinfo is None:
        raise ValueError(f"재시도 시각에 시간대가 없습니다 : {value!r}")
    return when


def _result_to_dict(result: ChannelAnalysisResult) -> dict[str, object]:
    return {
        "writing_style": list(result.writing_style),
        "channel_facts": list(result.channel_facts),
        "corrections": list(result.corrections),
        "note": result.note,
    }


def _as_strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TypeError(f"목록이 아닙니다 : {value!r}")
    return tuple(_as_str(item) for item in value)


def _as_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"사전이 아닙니다 : {value!r}")
    return value


def _result_from_dict(data: Mapping[str, object]) -> ChannelAnalysisResult:
    return ChannelAnalysisResult(
        writing_style=_as_strings(data["writing_style"]),
        channel_facts=_as_strings(data["channel_facts"]),
        corrections=_as_strings(data["corrections"]),
        note=_as_str(data["note"]),
    )


@dataclass(frozen=True)
class DayProgress:
    day: str
    completed: Mapping[str, ChannelAnalysisResult] = field(default_factory=dict)
    failures: Mapping[str, ChannelFailure] = field(default_factory=dict)
    notified: frozenset[str] = frozenset()
    #: Channels whose analysis has already gone out. Announcing only the round
    #: that just ran loses a channel whose delivery failed (sca-b4o review).
    announced: frozenset[str] = frozenset()
    #: False when the file existed but couldn't be read. An unreadable day must
    #: not look finished, or it drops out of the retry candidates (sca-b4o review).
    readable: bool = True

    def pending(self, channels: Iterable[str], now: datetime) -> tuple[str, ...]:
        def ready(name: str) -> bool:
            failure = self.failures.get(name)
            return failure is None or (failure.retryable and failure.due(now))

        return tuple(name for name in channels if name not in self.completed and ready(name))

    @property
    def settled(self) -> bool:
        """Whether the day can be marked done — nothing left worth retrying.

        A message that never went out counts as outstanding: marking the day
        done takes it out of the schedule's candidates for good, so the
        undelivered summary would never be retried (sca-b4o review).
        """
        if not self.readable:
            return False
        if self.unannounced_results() or self.unnotified_failures():
            return False
        return not any(failure.retryable for failure in self.failures.values())

    def waiting(self, now: datetime) -> bool:
        """Whether the day has retries left but none of them may run yet.

        A waiting day must yield its turn, or a day held open by a usage limit
        keeps older unfinished days from ever being picked (sca-b4o review). A
        message that has not gone out is work the day can do right now, so it
        is not waiting on anything.
        """
        if self.unannounced_results() or self.unnotified_failures():
            return False
        retryable = [failure for failure in self.failures.values() if failure.retryable]
        return bool(retryable) and not any(failure.due(now) for failure in retryable)

    def unnotified_failures(self) -> tuple[ChannelFailure, ...]:
        return tuple(f for f in self.failures.values() if f.key not in self.notified)

    def unannounced_results(self) -> Mapping[str, ChannelAnalysisResult]:
        return {name: r for name, r in self.completed.items() if name not in self.announced}

    def with_round(
        self,
        results: Mapping[str, ChannelAnalysisResult],
        failures: Iterable[ChannelFailure],
        now: datetime,
    ) -> DayProgress:
        completed = dict(self.completed)
        remaining = dict(self.failures)
        for name, result in results.items():
            completed[name] = result
            remaining.pop(name, None)
        for failure in failures:
            previous = self.failures.get(failure.channel)
            carried = previous.attempts if previous else 0
            attempts = carried if failure.kind.waits_for_approval else carried + failure.attempts
            remaining[failure.channel] = replace(
                failure, attempts=attempts,
                next_retry_at=now + failure.kind.retry_delay,
            )
        return replace(self, completed=completed, failures=remaining, readable=True)

    def restricted_to(self, channels: Iterable[str]) -> DayProgress:
        """Drops state for channels the day no longer has history for.

        A failure kept for a channel that disappeared from the archive can
        never be retried, so it would hold the day incomplete forever.
        """
        names = set(channels)
        return replace(
            self,
            completed={k: v for k, v in self.completed.items() if k in names},
            failures={k: v for k, v in self.failures.items() if k in names},
        )

    def with_reported(self) -> DayProgress:
        """Records what one delivered message covered — results and failures both."""
        return replace(
            self,
            notified=frozenset(f.key for f in self.failures.values()),
            announced=frozenset(self.completed),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "completed": {k: _result_to_dict(v) for k, v in self.completed.items()},
            "failures": {k: v.to_dict() for k, v in self.failures.items()},
            "notified": sorted(self.notified),
            "announced": sorted(self.announced),
        }

    @classmethod
    def from_dict(cls, day: str, data: Mapping[str, object]) -> DayProgress:
        completed = {
            _as_str(name): _result_from_dict(_as_mapping(value))
            for name, value in _as_mapping(data["completed"]).items()
        }
        failures = {
            _as_str(name): ChannelFailure.from_dict(_as_str(name), _as_mapping(value))
            for name, value in _as_mapping(data["failures"]).items()
        }
        overlap = sorted(set(completed) & set(failures))
        if overlap:
            # Such a channel is never re-analyzed yet holds the day open, so the
            # day is picked again on every tick with nothing to do (sca-b4o review).
            raise ValueError(f"완료와 실패에 함께 있는 채널 : {overlap}")
        return cls(
            day=day, completed=completed, failures=failures,
            notified=frozenset(_as_strings(data["notified"])),
            announced=frozenset(_as_strings(data["announced"])),
        )


class ProgressStore:
    """One JSON file per day, replaced atomically."""

    def __init__(self, progress_dir: Path) -> None:
        self._dir = progress_dir

    def _path(self, day: str) -> Path:
        return self._dir / f"{day}.progress.json"

    def load(self, day: str) -> DayProgress:
        path = self._path(day)
        if not path.exists():
            return DayProgress(day=day)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, Mapping):
                return DayProgress(day=day, readable=False)
            return DayProgress.from_dict(day, data)
        except (OSError, ValueError, KeyError, TypeError):
            # The worst case has to be re-analyzing channels that already
            # succeeded — never treating the day as finished (sca-b4o review).
            return DayProgress(day=day, readable=False)

    def is_waiting(self, day: str, now: datetime) -> bool:
        return self.load(day).waiting(now)

    def unsettled_days(self) -> tuple[str, ...]:
        """Days that still have something worth retrying, oldest first.

        The schedule only looks at today and yesterday on its own, so a day
        held open by a usage limit would roll out of range and never be
        retried once approval arrives (sca-b4o review).
        """
        if not self._dir.is_dir():
            return ()
        days = []
        for path in sorted(self._dir.glob("*.progress.json")):
            day = path.name.removesuffix(".progress.json")
            if not self.load(day).settled:
                days.append(day)
        return tuple(days)

    def save(self, progress: DayProgress) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(progress.day)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(progress.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return path
