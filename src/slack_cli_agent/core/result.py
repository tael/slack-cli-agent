"""Lookup result that distinguishes "absent" from "could not determine"."""

from __future__ import annotations

from enum import Enum
from typing import Generic, TypeVar

T = TypeVar("T")


class OutcomeKind(Enum):
    FOUND = "found"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class Outcome(Generic[T]):
    __slots__ = ("_kind", "_reason", "_value")

    def __init__(self, kind: OutcomeKind, value: T | None, reason: str) -> None:
        self._kind = kind
        self._value = value
        self._reason = reason

    @classmethod
    def found(cls, value: T) -> Outcome[T]:
        return cls(OutcomeKind.FOUND, value, "")

    @classmethod
    def absent(cls) -> Outcome[T]:
        return cls(OutcomeKind.ABSENT, None, "")

    @classmethod
    def unknown(cls, reason: str) -> Outcome[T]:
        return cls(OutcomeKind.UNKNOWN, None, reason)

    @property
    def kind(self) -> OutcomeKind:
        return self._kind

    @property
    def is_found(self) -> bool:
        return self._kind is OutcomeKind.FOUND

    @property
    def is_absent(self) -> bool:
        return self._kind is OutcomeKind.ABSENT

    @property
    def is_unknown(self) -> bool:
        return self._kind is OutcomeKind.UNKNOWN

    @property
    def reason(self) -> str:
        return self._reason

    def value(self) -> T:
        if self._kind is not OutcomeKind.FOUND:
            detail = f" {self._reason}" if self._reason else ""
            raise ValueError(f"값이 없다: {self._kind.value}{detail}")
        return self._value  # type: ignore[return-value]

    def value_or(self, default: T) -> T:
        # Only ABSENT gets the default; UNKNOWN still raises so a lookup failure
        # can't silently be treated as "not found".
        if self._kind is OutcomeKind.UNKNOWN:
            raise ValueError(f"판정 불가를 기본값으로 대체할 수 없다: {self._reason}")
        if self._kind is OutcomeKind.ABSENT:
            return default
        return self._value  # type: ignore[return-value]

    def __repr__(self) -> str:
        if self._kind is OutcomeKind.UNKNOWN:
            return f"Outcome.unknown({self._reason!r})"
        if self._kind is OutcomeKind.ABSENT:
            return "Outcome.absent()"
        return f"Outcome.found({self._value!r})"
