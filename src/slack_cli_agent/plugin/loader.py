"""Builds `BotPlugin` instances from the module paths in the profile config.

Path format is `"module.path"` or `"module.path:ClassName"`; without a
colon it looks for a `Plugin` attribute on the module.

Defaults to non-strict: a plugin that fails to load or has the wrong type
is skipped and reported in `PluginLoadResult.failures` rather than raising.
Losing a plugin only drops its org-specific features (admin commands,
prompt sections, permission rules) — the bot still works fine on the core
alone. This differs from a missing prompt file, which fails hard, because
that silently removes guard text from replies with no way to recover.
Set `strict=True` to raise on the first failure instead, for profiles where
a plugin is required.

A name collision between two plugins always fails, strict or not — it's a
data-corruption risk regardless of policy.
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from dataclasses import dataclass

from ..core.errors import ConfigError
from .base import BotPlugin

_DEFAULT_ATTR = "Plugin"


class PluginLoadError(ConfigError):
    pass


@dataclass(frozen=True)
class PluginLoadFailure:
    module_path: str
    reason: str


@dataclass(frozen=True)
class PluginLoadResult:
    plugins: tuple[BotPlugin, ...]
    failures: tuple[PluginLoadFailure, ...]

    @property
    def ok(self) -> bool:
        return not self.failures


class PluginLoader:
    def __init__(self, strict: bool = False) -> None:
        self._strict = strict

    def load(self, module_paths: Sequence[str]) -> PluginLoadResult:
        plugins: list[BotPlugin] = []
        failures: list[PluginLoadFailure] = []
        owner_of: dict[str, str] = {}

        for path in module_paths:
            try:
                plugin = self._load_one(path)
            except PluginLoadError as exc:
                if self._strict:
                    raise
                failures.append(PluginLoadFailure(module_path=path, reason=str(exc)))
                continue

            if plugin.name in owner_of:
                reason = (
                    f"플러그인 이름이 중복된다 : {plugin.name} "
                    f"({owner_of[plugin.name]} 와 {path})"
                )
                if self._strict:
                    raise PluginLoadError(reason)
                failures.append(PluginLoadFailure(module_path=path, reason=reason))
                continue

            owner_of[plugin.name] = path
            plugins.append(plugin)

        return PluginLoadResult(plugins=tuple(plugins), failures=tuple(failures))

    def _load_one(self, path: str) -> BotPlugin:
        module_path, _, attr = path.partition(":")
        target = attr or _DEFAULT_ATTR
        try:
            module = importlib.import_module(module_path)
        except ImportError as exc:
            raise PluginLoadError(
                f"플러그인 모듈을 불러오지 못했다 : {module_path} ({exc})"
            ) from exc

        obj = getattr(module, target, None)
        if obj is None:
            raise PluginLoadError(f"{module_path} 에 {target} 이 없다")
        if not (isinstance(obj, type) and issubclass(obj, BotPlugin)):
            raise PluginLoadError(f"{path} 은 BotPlugin 이 아니다 : {obj!r}")

        try:
            return obj()
        except Exception as exc:
            raise PluginLoadError(f"{path} 를 생성하지 못했다 : {exc}") from exc
