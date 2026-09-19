"""Channel settings, kept as a file so people can edit them and have the change apply without a restart."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..core.errors import ConfigError

#: Rendering and progress defaults, named once because the dataclass field and
#: from_dict() both need them -- they were two literals and drifted, so a
#: channels.json entry without the key came back False while the dataclass
#: default said True (sca-75v).
DEFAULT_RICH = True
DEFAULT_PROGRESS = True

KNOWN_KEYS = frozenset(
    {
        "name", "mode", "workdir", "model", "effort", "persona", "knowledge",
        "trusted_users", "answer_unaddressed", "session_scope",
        "disclose_mechanism", "skills", "light_context", "rich", "chat",
        "progress",
        "user_tools", "tool_enforcement",
    }
)
"""Channel-setting keys the core reads. Org-specific keys (like org_admins)
aren't here — plugins read those from `extra`."""

#: How hard an engine must enforce this channel's tool list. `audited` lets an
#: engine that can't hold an exact allowlist run anyway with the downgrade
#: recorded; `strict` refuses instead. Default is `audited` so the codex and
#: gemini bots keep working, and a channel that must be protected opts in.
TOOL_ENFORCEMENT_AUDITED = "audited"
TOOL_ENFORCEMENT_STRICT = "strict"
TOOL_ENFORCEMENT_LEVELS = (TOOL_ENFORCEMENT_AUDITED, TOOL_ENFORCEMENT_STRICT)

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
    user_tools: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    """Per-user extra tools. The org-specific value (an ops script, say) lives
    in the channel file, not in the installed package (sca-ww4)."""
    answer_unaddressed: bool = False
    session_scope: str = "thread"
    disclose_mechanism: bool = False
    skills: bool = False
    light_context: bool = False
    rich: bool = DEFAULT_RICH
    """Markdown blocks are the default rendering. The original bot.py had this
    on for one hardcoded channel; opting in per channel meant a bot with no
    channel file answered in stripped-down mrkdwn (sca-75v). A channel that
    needs plain text turns it off."""
    chat: str = CHAT_DEFAULT
    tool_enforcement: str = TOOL_ENFORCEMENT_AUDITED
    progress: bool = DEFAULT_PROGRESS
    """Whether to show progress for long-running work. On by default for the
    same reason as `rich`: opting in per channel meant the task card almost
    never appeared and a long request looked like nothing was happening
    (sca-stj). A channel that wants a quiet thread turns it off."""
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
            user_tools={
                str(user): tuple(str(tool) for tool in (tools or ()))
                for user, tools in (data.get("user_tools") or {}).items()
            },
            answer_unaddressed=bool(data.get("answer_unaddressed", False)),
            session_scope=str(data.get("session_scope") or "thread"),
            disclose_mechanism=bool(data.get("disclose_mechanism", False)),
            skills=bool(data.get("skills", False)),
            light_context=bool(data.get("light_context", False)),
            rich=bool(data.get("rich", DEFAULT_RICH)),
            chat=str(data.get("chat") or CHAT_DEFAULT),
            tool_enforcement=cls._tool_enforcement(channel_id, data),
            progress=bool(data.get("progress", DEFAULT_PROGRESS)),
            extra={k: v for k, v in data.items() if k not in KNOWN_KEYS},
        )

    @staticmethod
    def _tool_enforcement(channel_id: str, data: Mapping[str, Any]) -> str:
        """Refuses an unknown value instead of falling back. A typo in `strict`
        would otherwise read as the permissive default and drop enforcement
        without a word (sca-98k)."""
        level = str(data.get("tool_enforcement") or TOOL_ENFORCEMENT_AUDITED)
        if level not in TOOL_ENFORCEMENT_LEVELS:
            known = ", ".join(TOOL_ENFORCEMENT_LEVELS)
            raise ConfigError(f"채널 {channel_id} 의 tool_enforcement 값이 잘못됐다 : {level} (가능한 값: {known})")
        return level


class HasRich(Protocol):
    """Anything carrying the rendering flag. watchrunner declares its own
    narrow protocol rather than importing the whole config type."""

    @property
    def rich(self) -> bool: ...


def channel_is_rich(config: HasRich | None) -> bool:
    """Rendering mode for a channel that may not be registered. A bot answers a
    mention anywhere, so `config is None` means "not in channels.json", not
    "plain text" -- reading it as the latter stripped every answer of a bot with
    no channel file (sca-75v)."""
    return ChannelConfig(channel_id="").rich if config is None else config.rich


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
        just parse straight into ChannelConfig.

        A file that exists but can't be read raises. The callers replace the
        whole file, so reading a broken one as empty would drop every other
        channel's settings on the next command (sca-zvk). A missing file is
        different — that is the first registration.
        """
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(f"채널 설정을 읽지 못해 쓰기를 멈춘다 : {self._path} : {exc}") from exc
        if not isinstance(raw, Mapping):
            raise ConfigError(f"채널 설정의 최상위가 사전이 아니다 : {self._path}")
        return dict(raw)

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
