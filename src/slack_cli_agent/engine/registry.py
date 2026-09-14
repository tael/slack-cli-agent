"""엔진 등록소.

엔진 모듈이 자기를 등록한다. 어댑터 본체(호출부)는 Engine 만 안다. 세 번째
엔진을 추가하는 것은 모듈 하나를 만들고 이 등록소에 한 줄 등록하는 것으로
끝난다.

등록을 import 시점 부수 효과로 만들지 않는다. 조립하는 자리(Application)가
명시적으로 register() 를 부른다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..core.errors import ConfigError
from .base import Engine

if TYPE_CHECKING:
    from ..config.profile import Profile
    from ..config.settings import RuntimeSettings


class EngineRegistry:
    def __init__(self) -> None:
        self._classes: dict[str, type[Engine]] = {}

    def register(self, cls: type[Engine]) -> None:
        if not cls.name:
            raise ConfigError(f"{cls.__name__} 에 name 이 없다")
        self._classes[cls.name] = cls

    def create(self, name: str, profile: Profile, settings: RuntimeSettings) -> Engine:
        cls = self._classes.get(name)
        if cls is None:
            known = ", ".join(sorted(self._classes)) or "없음"
            raise ConfigError(f"엔진 {name} 이 등록돼 있지 않다. 등록된 엔진: {known}")
        return cls(profile, settings)

    def available(self) -> list[str]:
        return sorted(self._classes)
