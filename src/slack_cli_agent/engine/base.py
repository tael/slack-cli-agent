"""Engine interface and value objects.

This interface absorbs the differences between Claude and Codex — how
paths are passed, who issues session IDs, whether the system prompt is
pinned. Callers only know about Engine.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from ..auth.principal import TrustLevel
from ..core.errors import ConfigError

if TYPE_CHECKING:
    from ..config.profile import EngineSpec, Profile
    from ..config.settings import RuntimeSettings


@dataclass(frozen=True)
class EngineRequest:
    """One request to an engine. Channel/speaker decisions are already resolved by this point."""

    prompt: str
    system_prompt: str
    session_id: str
    resume: bool
    model: str
    effort: str
    workdir: Path
    readable_dirs: tuple[Path, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    trust_level: TrustLevel = TrustLevel.GENERAL


@dataclass(frozen=True)
class Usage:
    """Token usage for one engine turn.

    Field names are the common vocabulary; from_mapping() translates
    each engine's actual keys (e.g. cache_read_input_tokens) into them.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> Usage:
        if not isinstance(data, Mapping):
            return cls()
        return cls(
            input_tokens=int(data.get("input_tokens") or 0),
            output_tokens=int(data.get("output_tokens") or 0),
            cache_creation_tokens=int(data.get("cache_creation_input_tokens") or 0),
            cache_read_tokens=int(data.get("cache_read_input_tokens") or 0),
        )


@dataclass(frozen=True)
class UsageLimit:
    detail: str
    source: str  # "status_code" | "hint" | "subtype"


@dataclass(frozen=True)
class EngineResponse:
    ok: bool
    body: str
    session_id: str | None
    model_actual: str | None
    elapsed: float
    turns: int | None
    usage: Usage | None
    raw: Mapping[str, Any] = field(default_factory=dict)
    # nonzero_exit / bad_json / timeout / usage_limit / empty_response / is_error
    failure_reason: str | None = None


class Engine(ABC):
    """Contract for one engine. Has shared default implementations below, hence ABC rather than Protocol."""

    name: ClassVar[str] = ""

    def __init__(self, profile: Profile, settings: RuntimeSettings) -> None:
        self.profile = profile
        self.settings = settings

    @property
    def spec(self) -> EngineSpec:
        """The profile block for this engine's name.

        Whether this is wired as primary or secondary is decided by
        the profile's `type` field — the engine instance itself
        doesn't need to know which.
        """
        profile = self.profile
        if profile.primary_engine.type == self.name:
            return profile.primary_engine
        if profile.fallback_engine and profile.fallback_engine.type == self.name:
            return profile.fallback_engine
        raise ConfigError(f"프로필 {profile.name} 에 {self.name} 엔진 설정이 없다")

    @abstractmethod
    def build_command(self, request: EngineRequest) -> list[str]: ...

    @abstractmethod
    def parse(self, stdout: str, stderr: str, returncode: int) -> EngineResponse: ...

    @abstractmethod
    def new_session_id(self) -> str: ...

    @abstractmethod
    def detect_usage_limit(self, response: EngineResponse) -> UsageLimit | None: ...

    def session_id_from(self, response: EngineResponse) -> str | None:
        """Returns the engine's own session ID if it issues one, else None.

        None means "we already have an ID; there's nothing new to
        learn from this engine." Only engines like Codex's CLI, which
        mints its own thread ID, override this.
        """
        return None

    def prepare(self, request: EngineRequest) -> None:
        """Side effects the engine needs before running, e.g. writing its
        own config file. Separate from build_command() so building a
        command stays free of filesystem writes. Default: nothing."""

    def directives_for_turn(self, request: EngineRequest) -> str:
        """Per-turn directives, for engines with a pinned system prompt. Default: empty."""
        return ""

    def readable_paths_note(self, paths: Sequence[Path]) -> str:
        """How to announce readable paths, for engines that don't take it as an argument. Default: empty."""
        return ""
