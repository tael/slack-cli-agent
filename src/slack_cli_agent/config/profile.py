"""A single bot's definition — the boundary that keeps org-specific values out of code."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.errors import ConfigError
from .paths import StatePaths


@dataclass(frozen=True)
class EngineSpec:
    """How one engine is invoked. Grouped as a block so adding an engine
    doesn't grow the profile's top-level keys."""

    type: str
    binary: Path
    model: str
    model_owner: str = ""
    options: Mapping[str, Any] = field(default_factory=dict)
    # Engine-specific home path (e.g. CODEX_HOME). Keeps sessions/auth
    # separated per bot, so it can't collide with the user's own home.
    home_dir: Path | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EngineSpec:
        for key in ("type", "binary", "model"):
            if not data.get(key):
                raise ConfigError(f"엔진 설정에 {key} 가 없다")
        home_dir = data.get("home_dir")
        return cls(
            type=str(data["type"]),
            binary=Path(str(data["binary"])).expanduser(),
            model=str(data["model"]),
            model_owner=str(data.get("model_owner", "")),
            options=dict(data.get("options") or {}),
            home_dir=Path(str(home_dir)).expanduser() if home_dir else None,
        )

    def model_for_owner(self) -> str:
        return self.model_owner or self.model


@dataclass(frozen=True)
class Profile:
    name: str
    display_name: str
    primary_engine: EngineSpec
    fallback_engine: EngineSpec | None
    state_dir: Path
    work_root: Path
    data_dir: Path
    attach_dir: Path
    launch_label: str
    owner_user_id: str
    troubleshoot_channel: str
    owner_dm: str = ""
    plugins: tuple[str, ...] = ()
    settings_override: Mapping[str, Any] = field(default_factory=dict)

    @property
    def paths(self) -> StatePaths:
        return StatePaths(self.state_dir)

    @property
    def roster_file(self) -> Path:
        """Account handle to person-name table."""
        return self.data_dir / "roster.md"

    @classmethod
    def load(cls, name: str, search_paths: Sequence[Path]) -> Profile:
        """Finds `<name>.json` on search_paths in order and uses the first match."""
        for base in search_paths:
            candidate = base / f"{name}.json"
            if candidate.is_file():
                return cls.from_dict(json.loads(candidate.read_text(encoding="utf-8")))
        searched = ", ".join(str(p) for p in search_paths)
        raise ConfigError(f"프로필 {name} 을 찾지 못했다. 검색 경로: {searched}")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Profile:
        name = data.get("name")
        if not name:
            raise ConfigError("프로필에 name 이 없다")

        engine_block = data.get("primary_engine")
        if not isinstance(engine_block, Mapping):
            raise ConfigError("프로필에 primary_engine 블록이 없다")

        state_dir = Path(str(data.get("state_dir") or f"~/.{name}")).expanduser()
        fallback = data.get("fallback_engine")

        return cls(
            name=str(name),
            display_name=str(data.get("display_name") or name),
            primary_engine=EngineSpec.from_dict(engine_block),
            fallback_engine=EngineSpec.from_dict(fallback) if fallback else None,
            state_dir=state_dir,
            work_root=cls._under(data, "work_root", state_dir, "work"),
            data_dir=cls._under(data, "data_dir", state_dir, "data"),
            attach_dir=cls._under(data, "attach_dir", state_dir, "attachments"),
            launch_label=str(data.get("launch_label") or f"local.{name}"),
            owner_user_id=str(data.get("owner_user_id", "")),
            troubleshoot_channel=str(data.get("troubleshoot_channel", "")),
            owner_dm=str(data.get("owner_dm", "")),
            plugins=tuple(data.get("plugins") or ()),
            settings_override=dict(data.get("settings") or {}),
        )

    @staticmethod
    def _under(data: Mapping[str, Any], key: str, state_dir: Path, name: str) -> Path:
        given = data.get(key)
        if given:
            return Path(str(given)).expanduser()
        return state_dir / name

    def validate(self) -> list[str]:
        """Config problems; an empty list means the profile is valid."""
        problems: list[str] = []
        if not self.owner_user_id:
            problems.append("owner_user_id 가 비어 있다")
        if not self.troubleshoot_channel:
            problems.append("troubleshoot_channel 이 비어 있다")
        for label, spec in self._engines():
            if spec.binary.is_absolute() and not spec.binary.exists():
                problems.append(f"{label} 엔진 실행 파일이 없다: {spec.binary}")
        if self.fallback_engine and self.fallback_engine.type == self.primary_engine.type:
            problems.append("폴백 엔진이 1차 엔진과 같은 종류다")
        return problems

    def _engines(self) -> list[tuple[str, EngineSpec]]:
        pairs = [("1차", self.primary_engine)]
        if self.fallback_engine:
            pairs.append(("2차", self.fallback_engine))
        return pairs
