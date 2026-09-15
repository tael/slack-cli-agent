"""Engine interface and value objects.

This interface absorbs the differences between Claude and Codex — how
paths are passed, who issues session IDs, whether the system prompt is
pinned. Callers only know about Engine.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from ..auth.principal import TrustLevel
from ..core.errors import ConfigError
from .environment import EngineEnvironmentPolicy, create_environment_policy

if TYPE_CHECKING:
    from ..config.profile import EngineSpec, Profile
    from ..config.settings import RuntimeSettings


class ElapsedSource(StrEnum):
    """Where EngineResponse.elapsed came from.

    A plain str let a typo or a new source name pass runner.py's
    ``== "unknown"`` check silently — mypy catches that now, since
    assigning a str literal where ElapsedSource is expected is a type
    error even though StrEnum members still compare equal to str.
    """

    ENGINE = "engine"
    """The CLI itself reported elapsed."""
    RUNNER = "runner"
    """EngineRunner measured wall-clock time because the engine didn't."""
    UNKNOWN = "unknown"
    """Neither has run yet, e.g. a response built directly in a test."""


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


_USAGE_FIELDS: tuple[str, ...] = (
    "input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens",
)


@dataclass(frozen=True)
class Usage:
    """Token usage for one engine turn.

    Field names are the common vocabulary; from_native() translates each
    engine's actual keys (e.g. cache_read_input_tokens) into them via a
    per-engine key_map.

    A missing native key isn't the same as a measured zero (sca-dyb.4) — it
    means this engine's output can't answer that field, not that it did zero
    of it. `unavailable` names which common fields that turn was for, and
    participates in equality like every other field: a turn whose usage is
    partly unreadable is not the same Usage as one that measured everything
    as zero, and a test comparing the two should see that difference rather
    than have it hidden. Use equivalent_values() below when a test only
    cares about the counted numbers, not which fields could be measured.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    unavailable: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_native(cls, data: Mapping[str, Any] | None, key_map: Mapping[str, str]) -> Usage:
        """Translates another engine's native usage dict via key_map.

        key_map maps a common field name (one of _USAGE_FIELDS) to that
        engine's own key for it. A common field with no entry in key_map, or
        whose native key isn't present in data, is recorded in unavailable
        instead of being read as a measured zero.
        """
        if not isinstance(data, Mapping):
            return cls(unavailable=frozenset(_USAGE_FIELDS))
        values: dict[str, int] = {}
        unavailable: set[str] = set()
        for field_name in _USAGE_FIELDS:
            native_key = key_map.get(field_name)
            if native_key is None or native_key not in data:
                unavailable.add(field_name)
                values[field_name] = 0
                continue
            values[field_name] = int(data.get(native_key) or 0)
        return cls(**values, unavailable=frozenset(unavailable))


    def as_audit_dict(self) -> dict[str, Any]:
        """JSON-safe form for the audit record. Lives here rather than at the
        call site because every engine goes through the same audit path, and a
        frozenset leaking into json.dumps dropped the whole request (sca-kwv).
        """
        record: dict[str, Any] = {name: getattr(self, name) for name in _USAGE_FIELDS}
        record["unavailable"] = sorted(self.unavailable)
        return record


def equivalent_values(a: Usage, b: Usage) -> bool:
    """Compares only the four counted fields, ignoring which side could
    actually measure them.

    For tests built around a fixture usage dict that happens not to cover
    every field — they want to know the numbers it does have line up, not
    whether both sides agree on what's unavailable.
    """
    return tuple(getattr(a, name) for name in _USAGE_FIELDS) == tuple(getattr(b, name) for name in _USAGE_FIELDS)


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
    # sca-cfa — Codex's JSONL output has no duration field at all, so its
    # elapsed_source stays UNKNOWN until EngineRunner fills it from wall-clock time.
    elapsed_source: ElapsedSource = ElapsedSource.UNKNOWN
    # Name of the engine that actually produced this response. EngineRunner fills
    # it in, so a fallback answer carries the secondary's name rather than the
    # primary's — reporting needs it to pick the matching transcript format.
    engine: str = ""


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

    def environment_policy(self) -> EngineEnvironmentPolicy:
        """Isolation policy for this engine's own subprocess.

        Lives on the engine, not the runner: a fallback turn runs a different
        engine with a different home dir, and one policy shared across both
        would point the secondary's CLI at the primary's home.
        """
        return create_environment_policy(self.name, self.profile.name, self.spec.home_dir)

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
