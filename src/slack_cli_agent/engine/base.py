"""엔진 인터페이스와 값 객체.

Claude 와 Codex 의 차이(경로 전달 방식, 세션 발급 주체, 시스템 프롬프트 고정
여부)를 이 인터페이스가 흡수한다. 호출부는 Engine 하나만 안다.
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
    """엔진에 넘길 요청 하나. 채널·화자 판단은 이미 끝난 뒤의 값이다."""

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
    """엔진 실행 한 턴의 토큰 사용량.

    필드 이름은 공통 이름으로 두고, 각 엔진의 실제 키(cache_read_input_tokens
    등)에서 from_mapping() 이 변환한다.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "Usage":
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
    """구독 한도 소진 판정 결과."""

    detail: str
    source: str  # "status_code" | "hint" | "subtype"


@dataclass(frozen=True)
class EngineResponse:
    """엔진 실행 결과. build_command 로 연 프로세스의 출력을 이 형식으로 통일한다."""

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
    """엔진 하나의 계약. 공유 기본 구현이 있어 ABC 로 둔다."""

    name: ClassVar[str] = ""

    def __init__(self, profile: "Profile", settings: "RuntimeSettings") -> None:
        self.profile = profile
        self.settings = settings

    @property
    def spec(self) -> "EngineSpec":
        """이 엔진 이름에 해당하는 프로필 블록.

        1차·2차 어느 쪽에 배정됐는지는 프로필의 type 값으로 가린다. 엔진
        인스턴스 자신은 자기가 1차인지 2차인지 모른다 — 몰라도 되게 짠다.
        """
        profile = self.profile
        if profile.primary_engine.type == self.name:
            return profile.primary_engine
        if profile.fallback_engine and profile.fallback_engine.type == self.name:
            return profile.fallback_engine
        raise ConfigError(f"프로필 {profile.name} 에 {self.name} 엔진 설정이 없다")

    @abstractmethod
    def build_command(self, request: EngineRequest) -> list[str]:
        """실행할 명령줄."""

    @abstractmethod
    def parse(self, stdout: str, stderr: str, returncode: int) -> EngineResponse:
        """실행 결과를 공통 형식으로."""

    @abstractmethod
    def new_session_id(self) -> str:
        """이 엔진의 세션 식별자 발급 방식."""

    @abstractmethod
    def detect_usage_limit(self, response: EngineResponse) -> UsageLimit | None:
        """한도 소진 판정."""

    def session_id_from(self, response: EngineResponse) -> str | None:
        """엔진이 자기 세션 ID 를 발급하면 그것을 돌려준다. 기본은 None.

        기본값은 "우리가 이미 정한 ID 외에 새로 배울 것이 없다"는 뜻이다.
        Codex 처럼 CLI 가 스스로 세션(스레드) ID 를 매기는 엔진만 이 값을
        재정의해 실제 ID 를 돌려준다.
        """
        return None

    def directives_for_turn(self, request: EngineRequest) -> str:
        """턴마다 바뀌는 지시. 시스템 프롬프트가 고정되는 엔진용. 기본은 빈 문자열."""
        return ""

    def readable_paths_note(self, paths: Sequence[Path]) -> str:
        """읽기 허용 경로를 알리는 방식. 인자로 되는 엔진은 빈 문자열."""
        return ""
