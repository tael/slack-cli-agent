"""Engine registry.

Each engine module registers itself; callers only know about Engine.
Adding a third engine means writing one module and one register()
call here.

Registration isn't an import-time side effect. The assembly layer
(Application) calls register() explicitly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..core.errors import ConfigError
from .base import Engine

log = logging.getLogger(__name__)

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

    def engine_class(self, name: str) -> type[Engine] | None:
        """For callers that need an engine's declarations without building one —
        preflight reads `capabilities` before any profile is loaded into one."""
        return self._classes.get(name)

    def available(self) -> list[str]:
        return sorted(self._classes)


def default_registry() -> EngineRegistry:
    """The engines this package ships. Plugins add their own on top of this.

    Here rather than in the assembly layer so preflight can read the same set
    without importing Application.
    """
    from .claude import ClaudeEngine
    from .codex import CodexEngine
    from .gemini import GeminiEngine

    registry = EngineRegistry()
    registry.register(ClaudeEngine)
    registry.register(CodexEngine)
    registry.register(GeminiEngine)
    return registry


def registry_for_profile(profile: Profile) -> EngineRegistry:
    """default_registry() plus the engines this profile's plugins declare.

    For readers outside the assembly layer that only hold a profile. Without it
    a plugin engine looks unknown and its declarations are read as another
    engine's defaults (sca-cs0).

    A plugin that fails is skipped, like everywhere else plugins are loaded.
    Callers here are read paths -- the web console polls one every few seconds
    -- so one broken plugin must not take the whole reply down.
    """
    from ..plugin.loader import PluginLoader

    registry = default_registry()
    for plugin in PluginLoader().load(profile.plugins).plugins:
        try:
            for engine_class in plugin.engines():
                registry.register(engine_class)
        except Exception as exc:  # noqa: BLE001 - a broken plugin loses its engines, nothing else
            log.warning("플러그인 %s 의 엔진을 등록하지 못했다 : %s", plugin.name, exc)
    return registry
