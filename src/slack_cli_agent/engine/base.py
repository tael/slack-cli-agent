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
from .capability import EngineCapabilities, ExecutionRequirements
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


class CallOrigin(StrEnum):
    """Whether a human is waiting on this turn.

    Only the fallback layer reads it. The recovery probe sends one real request
    to the primary to see if it came back, and a failed probe pushes the next
    attempt out by the probe interval — so a background caller that consumes it
    makes a human wait out that interval for a recovery they would otherwise
    have gotten (sca-dyb.9 review).

    Interactive is the default because that is the failure that hurts: a
    background caller that forgets loses one probe, an interactive one that
    forgets means recovery never reaches a human at all.
    """

    INTERACTIVE = "interactive"
    BACKGROUND = "background"


@dataclass(frozen=True)
class EngineRequest:
    """One request to an engine. Channel/speaker decisions are already resolved by this point."""

    prompt: str
    system_prompt: str
    #: None means the engine that ends up running this mints one. Which engine
    #: that is isn't settled until EngineRunner.run(), so callers can't pick the
    #: format themselves (sca-56y).
    session_id: str | None
    resume: bool
    #: None means the engine that ends up running this uses its own spec.model.
    #: Model names differ per engine, and which engine runs isn't settled until
    #: EngineRunner.run() (sca-dyb.10).
    model: str | None
    effort: str
    workdir: Path
    readable_dirs: tuple[Path, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    trust_level: TrustLevel = TrustLevel.GENERAL
    #: What this request needs the engine to enforce. Empty means no
    #: requirement -- recorded either way, so "no requirement" and "nothing
    #: recorded" stay distinguishable in the audit.
    requirements: ExecutionRequirements = field(default_factory=ExecutionRequirements)
    #: Where this engine should append the name of each tool it starts, for
    #: the progress display. None means this request has no display — either
    #: the channel has it off, or no one is watching the file. How the file
    #: gets written is per-engine (claude uses a PreToolUse hook); an engine
    #: with no equivalent ignores this and the display shows its opening
    #: line only, rather than failing the request.
    progress_log: Path | None = None

    def require_session_id(self) -> str:
        """For build_command, which only ever runs after EngineRunner filled it in.

        Raising rather than minting one here: this class doesn't know which
        engine is running, and guessing the format is what broke the watch
        check in the first place (sca-56y).
        """
        # Empty counts as missing: an empty --session-id reaches the CLI as a
        # present-but-blank argument, which is harder to trace than a raise.
        if not self.session_id:
            raise ValueError("세션 ID 가 아직 정해지지 않았습니다. EngineRunner.run 을 거쳐야 합니다.")
        return self.session_id

    def require_model(self) -> str:
        """For build_command, which only ever runs after EngineRunner filled it in."""
        # Empty counts as missing, same as session_id: a blank --model argument
        # reaches the CLI as present-but-empty rather than raising here.
        if not self.model:
            raise ValueError("모델이 아직 정해지지 않았습니다. EngineRunner.run 을 거쳐야 합니다.")
        return self.model


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


# Values these CLIs are known to put in the fields we read as enums. Anything
# else is reported as its length only: the prompt and the watch condition reach
# every CLI as argv, so a field we read as an enum can hold conversation text,
# a token or a path. A charset filter would pass all three through unchanged,
# so this is an allowlist rather than a denylist (sca-dyb.14).
KNOWN_DETAIL_CODES = frozenset(
    {
        # claude: how a usage limit was detected
        "status_code", "hint", "subtype",
        # claude: result subtype
        "success", "error_max_turns", "error_max_limit", "error_during_execution",
        # gemini: response status
        "SUCCESS", "ERROR",
        # runner: engine switch approval state
        "pending", "approved", "denied",
    }
)


@dataclass(frozen=True)
class FailureDetail:
    """Log-safe companion to failure_reason.

    Numbers the engine counted itself, plus one classification drawn from
    KNOWN_DETAIL_CODES. A plain str field would not force callers through any
    of this, so EngineResponse holds this type instead.
    """

    exit_code: int | None = None
    stdout_chars: int | None = None
    timeout_sec: int | None = None
    tool_errors: int | None = None
    code: str = ""

    def __post_init__(self) -> None:
        for name in ("exit_code", "stdout_chars", "timeout_sec", "tool_errors"):
            value = getattr(self, name)
            if value is None:
                continue
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{name} 은 정수여야 한다 : {type(value).__name__}")
        if not isinstance(self.code, str):
            raise TypeError(f"code 는 문자열이어야 한다 : {type(self.code).__name__}")

    def _code_text(self) -> str:
        return self.code if self.code in KNOWN_DETAIL_CODES else f"unknown:{len(self.code)}"

    def __bool__(self) -> bool:
        return any(
            value is not None
            for value in (self.exit_code, self.stdout_chars, self.timeout_sec, self.tool_errors)
        ) or bool(self.code)

    def __str__(self) -> str:
        parts = [
            f"{name}={value}"
            for name, value in (
                ("exit_code", self.exit_code),
                ("stdout_chars", self.stdout_chars),
                ("timeout_sec", self.timeout_sec),
                ("tool_errors", self.tool_errors),
            )
            if value is not None
        ]
        if self.code:
            parts.append(f"code={self._code_text()}")
        return " ".join(parts)


NO_DETAIL = FailureDetail()


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
    failure_detail: FailureDetail = NO_DETAIL
    # sca-cfa — Codex's JSONL output has no duration field at all, so its
    # elapsed_source stays UNKNOWN until EngineRunner fills it from wall-clock time.
    elapsed_source: ElapsedSource = ElapsedSource.UNKNOWN
    # Name of the engine that actually produced this response. EngineRunner fills
    # it in, so a fallback answer carries the secondary's name rather than the
    # primary's — reporting needs it to pick the matching transcript format.
    engine: str = ""

    def __post_init__(self) -> None:
        # Type annotations alone stop nobody at runtime, and this field is what
        # reaches the log and the audit record (sca-dyb.14).
        if not isinstance(self.failure_detail, FailureDetail):
            raise TypeError(
                f"failure_detail 은 FailureDetail 이어야 한다 : {type(self.failure_detail).__name__}"
            )


class Engine(ABC):
    """Contract for one engine. Has shared default implementations below, hence ABC rather than Protocol."""

    name: ClassVar[str] = ""

    #: What this engine can enforce at best. The actual guarantee for one
    #: request is capabilities_for() -- codex's sandbox comes from profile
    #: options, and claude's allowlist only holds if the request has one.
    capabilities: ClassVar[EngineCapabilities] = EngineCapabilities()

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

    def capabilities_for(self, request: EngineRequest) -> EngineCapabilities:
        """This request's actual guarantee. Default: the class declaration."""
        return self.capabilities

    def prepare(self, request: EngineRequest) -> None:  # noqa: B027 — 기본이 아무것도 안 하는 것이다. 추상으로 두면 필요 없는 엔진까지 빈 정의를 쓰게 된다
        """Side effects the engine needs before running, e.g. writing its
        own config file. Separate from build_command() so building a
        command stays free of filesystem writes. Default: nothing."""

    def directives_for_turn(self, request: EngineRequest) -> str:
        """Per-turn directives, for engines with a pinned system prompt. Default: empty."""
        return ""

    def readable_paths_note(self, paths: Sequence[Path]) -> str:
        """How to announce readable paths, for engines that don't take it as an argument. Default: empty."""
        return ""
