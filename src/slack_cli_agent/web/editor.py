"""웹 콘솔이 설정을 읽고 고칠 때 쓰는 계층. 검증과 쓰기는 기존 모듈을 재사용한다."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from ..config.channel import ChannelConfig, ChannelRegistry
from ..config.profile import Profile
from ..core.errors import ConfigError


class ProfileEditor:
    def __init__(self, search_dirs: Sequence[Path]) -> None:
        self._search_dirs = list(search_dirs)

    def names(self) -> list[str]:
        return Profile.discover(self._search_dirs)

    def read(self, name: str) -> dict[str, object]:
        path = self._find(name)
        if path is None:
            raise ConfigError(f"프로필 {name} 을 찾지 못했다")
        return dict(json.loads(path.read_text(encoding="utf-8")))

    def save(self, name: str, data: Mapping[str, object]) -> list[str]:
        errors: list[str] = []
        try:
            profile = Profile.from_dict(data)
        except ConfigError as e:
            return [str(e)]
        errors.extend(profile.validate())
        if errors:
            return errors

        path = self._find(name)
        if path is None:
            path = self._search_dirs[0] / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(dict(data), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return []

    def _find(self, name: str) -> Path | None:
        for base in self._search_dirs:
            candidate = base / f"{name}.json"
            if candidate.is_file():
                return candidate
        return None


class ChannelEditor:
    def __init__(self, registry: ChannelRegistry) -> None:
        self._registry = registry

    def list(self) -> list[dict[str, object]]:
        return [self._to_dict(config) for config in self._registry.all().values()]

    def update(self, channel_id: str, changes: Mapping[str, object]) -> dict[str, object]:
        config = self._registry.update(channel_id, changes)
        return self._to_dict(config)

    @staticmethod
    def _to_dict(config: ChannelConfig) -> dict[str, object]:
        entry: dict[str, object] = {
            "channel_id": config.channel_id,
            "name": config.name,
            "mode": config.mode,
            "workdir": str(config.workdir) if config.workdir else "",
            "model": config.model,
            "effort": config.effort,
            "persona": config.persona,
            "knowledge": list(config.knowledge),
            "trusted_users": sorted(config.trusted_users),
            "answer_unaddressed": config.answer_unaddressed,
            "session_scope": config.session_scope,
            "disclose_mechanism": config.disclose_mechanism,
            "skills": config.skills,
            "light_context": config.light_context,
            "rich": config.rich,
            "chat": config.chat,
            "progress": config.progress,
        }
        entry.update(config.extra)
        return entry
