"""Engine registry.

Each engine module registers itself; callers only know about Engine.
Adding a third engine means writing one module and one register()
call here.

Registration isn't an import-time side effect. The assembly layer
(Application) calls register() explicitly.
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
