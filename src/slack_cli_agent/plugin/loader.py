"""설정에 적힌 모듈 경로에서 `BotPlugin` 을 만든다.

경로 형식은 `"module.path"` 또는 `"module.path:ClassName"` 이다. 콜론이
없으면 모듈의 `Plugin` 속성을 찾는다.

**적재 실패 처리 방침과 그 근거.** 기본은 `strict=False` — 모듈이 없거나
타입이 틀려도 그 플러그인 하나만 빠지고 나머지는 그대로 적재되며, 예외를
던지지 않고 `PluginLoadResult.failures` 에 담아 돌려준다. 이유는 플러그인이
빠졌을 때 잃는 것과, `PromptFileCheck`(프리플라이트)가 프롬프트 파일 부재를
막는 이유가 다르기 때문이다 — 프롬프트 파일이 없으면 가드 문구 자체가
빠진 채 답이 나가 되돌릴 수 없지만(그래서 `MissingPromptError` 로 무조건
막는다), 플러그인 하나가 없으면 그 조직 전용 기능(관리 명령·프롬프트
조각·권한 조건)만 빠질 뿐 코어 동작에는 영향이 없다. 코어만으로도 봇은
정상 동작하므로, 그 정도로 전체 기동을 막을 이유가 없다고 판단했다.
`strict=True` 로 켜면 첫 실패에서 바로 예외를 던진다 — 운영 환경에서
"플러그인이 반드시 있어야 한다"고 판단되면 그 프로필에서 켠다.

이름 충돌(같은 `BotPlugin.name` 을 가진 플러그인 둘)은 정책과 무관하게
데이터 오염 위험이라 늘 실패로 본다 — strict 에서는 예외, 아니면 실패
목록에 담고 나중 것을 버린다.
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from dataclasses import dataclass

from ..core.errors import ConfigError
from .base import BotPlugin

_DEFAULT_ATTR = "Plugin"


class PluginLoadError(ConfigError):
    """플러그인 모듈을 불러오지 못했거나 타입이 맞지 않는다."""


@dataclass(frozen=True)
class PluginLoadFailure:
    """플러그인 하나를 적재하지 못한 사유."""

    module_path: str
    reason: str


@dataclass(frozen=True)
class PluginLoadResult:
    """적재를 마친 뒤의 전체 결과."""

    plugins: tuple[BotPlugin, ...]
    failures: tuple[PluginLoadFailure, ...]

    @property
    def ok(self) -> bool:
        return not self.failures


class PluginLoader:
    """`Profile.plugins` 에 적힌 모듈 경로들을 실제 `BotPlugin` 인스턴스로 만든다."""

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
