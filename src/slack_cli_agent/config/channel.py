"""Channel settings, kept as a file so people can edit them and have the change apply without a restart."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

KNOWN_KEYS = frozenset(
    {
        "name", "mode", "workdir", "model", "effort", "persona", "knowledge",
        "trusted_users", "answer_unaddressed", "session_scope",
        "disclose_mechanism", "skills", "light_context", "rich", "chat",
        "progress",
    }
)
"""Channel-setting keys the core reads. Org-specific keys (like org_admins)
aren't here — plugins read those from `extra`."""

CHAT_DEFAULT = "normal"
"""Default chat volume, used when assembling prompts."""


@dataclass(frozen=True)
class ChannelConfig:
    channel_id: str
    name: str = ""
    """Channel name from Slack, for display. Falls back to the channel ID
    on lookup failure, filled in at read time so it's never blank."""
    mode: str = "default"
    workdir: Path | None = None
    model: str = ""
    effort: str = ""
    persona: str = ""
    knowledge: tuple[str, ...] = ()
    trusted_users: frozenset[str] = frozenset()
    answer_unaddressed: bool = False
    session_scope: str = "thread"
    disclose_mechanism: bool = False
    skills: bool = False
    light_context: bool = False
    rich: bool = False
    chat: str = CHAT_DEFAULT
    progress: bool = False
    """Whether to stream progress updates for long-running work."""
    extra: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, channel_id: str, data: Mapping[str, Any]) -> ChannelConfig:
        workdir = data.get("workdir")
        return cls(
            channel_id=channel_id,
            name=str(data.get("name") or channel_id),
            mode=str(data.get("mode", "default")),
            workdir=Path(str(workdir)).expanduser() if workdir else None,
            model=str(data.get("model", "")),
            effort=str(data.get("effort", "")),
            persona=str(data.get("persona", "")),
            knowledge=tuple(data.get("knowledge") or ()),
            trusted_users=frozenset(data.get("trusted_users") or ()),
            answer_unaddressed=bool(data.get("answer_unaddressed", False)),
            session_scope=str(data.get("session_scope") or "thread"),
            disclose_mechanism=bool(data.get("disclose_mechanism", False)),
            skills=bool(data.get("skills", False)),
            light_context=bool(data.get("light_context", False)),
            rich=bool(data.get("rich", False)),
            chat=str(data.get("chat") or CHAT_DEFAULT),
            progress=bool(data.get("progress", False)),
            extra={k: v for k, v in data.items() if k not in KNOWN_KEYS},
        )


class ChannelRegistry:
    """Re-reads the channel-config file on every access, but only reparses
    when mtime changes, so hand-edits apply without a restart."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._mtime: float = -1.0
        self._configs: dict[str, ChannelConfig] = {}

    def get(self, channel_id: str) -> ChannelConfig | None:
        return self._current().get(channel_id)

    def is_registered(self, channel_id: str) -> bool:
        return channel_id in self._current()

    def all(self) -> dict[str, ChannelConfig]:
        return dict(self._current())

    def channel_ids(self) -> list[str]:
        return list(self._current())

    def update(self, channel_id: str, changes: Mapping[str, Any]) -> ChannelConfig:
        """Merges changes into one channel's config, creating it if missing.
        Unknown keys are preserved since plugins may own them."""
        with self._lock:
            raw = self._read_raw()
            entry = dict(raw.get(channel_id) or {})
            entry.update(changes)
            raw[channel_id] = entry
            self._write_raw(raw)
            return self._configs[channel_id]

    def remove(self, channel_id: str) -> bool:
        """Unregisters a channel. Returns False if it wasn't registered."""
        with self._lock:
            raw = self._read_raw()
            if channel_id not in raw:
                return False
            del raw[channel_id]
            self._write_raw(raw)
            return True

    def _read_raw(self) -> dict[str, Any]:
        """Reads the file as-is; unknown keys must survive, so this can't
        just parse straight into ChannelConfig."""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return dict(raw) if isinstance(raw, Mapping) else {}

    def _write_raw(self, raw: Mapping[str, Any]) -> None:
        """Writes to a temp file and replaces atomically, so a reader never
        sees a truncated write mid-edit — only the old file or the new one,
        never both.

        Updates the cache directly afterward: on filesystems with 1-second
        mtime resolution, two writes in the same second wouldn't otherwise
        be noticed.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        임시 = self._path.with_name(self._path.name + ".tmp")
        임시.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(임시, self._path)
        self._configs = {
            cid: ChannelConfig.from_dict(cid, data)
            for cid, data in raw.items()
            if isinstance(data, Mapping)
        }
        try:
            self._mtime = self._path.stat().st_mtime
        except OSError:
            self._mtime = -1.0

    def _current(self) -> dict[str, ChannelConfig]:
        with self._lock:
            try:
                mtime = self._path.stat().st_mtime
            except OSError:
                self._mtime, self._configs = -1.0, {}
                return self._configs
            if mtime != self._mtime:
                self._configs = self._parse()
                self._mtime = mtime
            return self._configs

    def _parse(self) -> dict[str, ChannelConfig]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A file mid-save can briefly be invalid JSON; keep the last
            # good config instead of failing. mtime changes again once the
            # edit finishes, so the fix applies on the next read.
            return dict(self._configs)
        if not isinstance(raw, Mapping):
            return {}
        return {
            cid: ChannelConfig.from_dict(cid, data)
            for cid, data in raw.items()
            if isinstance(data, Mapping)
        }
