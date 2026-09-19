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

    @classmethod
    def unrestricted(cls) -> ToolSelection:
        return cls()

    @classmethod
    def allow(cls, names: Sequence[str]) -> ToolSelection:
        if not names:
            raise ValueError(
                "허용할 도구 이름이 비어 있다. 도구를 쓰지 않으려면 forbid_all() 을 쓴다"
            )
        return cls(access=ToolAccess.ALLOWLIST, names=tuple(names))

    @classmethod
    def forbid_all(cls) -> ToolSelection:
        return cls(access=ToolAccess.FORBIDDEN)

    @property
    def restriction(self) -> ToolRestriction:
        return _RESTRICTION[self.access]
