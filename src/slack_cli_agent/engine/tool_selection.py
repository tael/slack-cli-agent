# Which tools one request may use. Three states rather than a list, because a
# list cannot tell "nobody named any tools" from "this turn must have none".
# An empty tuple meant both, and the nightly learning batch lost that bet: its
# comment said no tools while the CLI ran it with every tool open (sca-0a7).

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from .capability import ToolRestriction


class ToolAccess(StrEnum):
    UNRESTRICTED = "unrestricted"
    ALLOWLIST = "allowlist"
    FORBIDDEN = "forbidden"


_RESTRICTION: dict[ToolAccess, ToolRestriction] = {
    ToolAccess.UNRESTRICTED: ToolRestriction.NONE,
    ToolAccess.ALLOWLIST: ToolRestriction.EXACT_ALLOWLIST,
    ToolAccess.FORBIDDEN: ToolRestriction.ALL_FORBIDDEN,
}


@dataclass(frozen=True)
class ToolSelection:
    """What the caller asked for. What an engine holds is ToolRestriction."""

    access: ToolAccess = ToolAccess.UNRESTRICTED
    names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # frozen keeps the value from changing; it does not keep a caller from
        # building a contradictory one through the constructor or
        # dataclasses.replace (review 2026-09-19).
        if self.access is ToolAccess.ALLOWLIST and not self.names:
            raise ValueError(
                "허용할 도구 이름이 비어 있다. 도구를 쓰지 않으려면 forbid_all() 을 쓴다"
            )
        if self.access is not ToolAccess.ALLOWLIST and self.names:
            raise ValueError(f"{self.access} 상태에는 도구 이름을 둘 수 없다 : {self.names}")

    @classmethod
    def unrestricted(cls) -> ToolSelection:
        return cls()

    @classmethod
    def allow(cls, names: Sequence[str]) -> ToolSelection:
        return cls(access=ToolAccess.ALLOWLIST, names=tuple(names))

    @classmethod
    def from_names(cls, names: Sequence[str]) -> ToolSelection:
        """What a tool policy produced. No names is not a ban -- only a caller
        that means "this turn has no tools" says forbid_all()."""
        return cls.allow(names) if names else cls.unrestricted()

    @classmethod
    def forbid_all(cls) -> ToolSelection:
        return cls(access=ToolAccess.FORBIDDEN)

    @property
    def restriction(self) -> ToolRestriction:
        return _RESTRICTION[self.access]
