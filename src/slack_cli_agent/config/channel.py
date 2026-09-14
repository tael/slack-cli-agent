"""채널 설정. 파일로 두어 사람이 편집하고 재기동 없이 반영되게 한다."""

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
"""코어가 읽는 채널 설정 키.

원본 bot.py 에서 실제로 조회되는 키를 실측해 정했다. 조직 고유 키
(org_admins 등)는 여기 넣지 않는다 — 플러그인이 extra 에서 읽는다.
"""

CHAT_DEFAULT = "normal"
"""채널의 대화량. 원본 CHAT_DEFAULT 와 같은 값이다. 프롬프트 조립에 쓰인다."""


@dataclass(frozen=True)
class ChannelConfig:
    channel_id: str
    name: str = ""
    """슬랙에서 조회한 채널 이름. 목록 표시에 쓴다.

    조회에 실패하면 원본과 같이 채널 ID 를 그대로 쓴다. 읽을 때 채우므로 이
    필드가 비어 있는 상태로 나오지 않는다.
    """
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
    """긴 작업의 중간 단계를 흘려 보낼지. 채널마다 켜고 끈다."""
    extra: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, channel_id: str, data: Mapping[str, Any]) -> "ChannelConfig":
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
    """채널 설정 파일을 요청마다 다시 읽는다.

    mtime 이 바뀌었을 때만 파싱하므로 매 요청 읽어도 부담이 없고, 사람이 파일을
    고치면 재기동 없이 반영된다.
    """

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
        """채널 설정의 일부 항목만 바꾼다. 없는 채널이면 새로 만든다.

        알 수 없는 키는 그대로 둔다 — 플러그인이 쓰는 항목을 코어의 쓰기가
        지우면 안 된다. 바뀐 설정을 돌려준다.
        """
        with self._lock:
            raw = self._read_raw()
            entry = dict(raw.get(channel_id) or {})
            entry.update(changes)
            raw[channel_id] = entry
            self._write_raw(raw)
            return self._configs[channel_id]

    def remove(self, channel_id: str) -> bool:
        """채널 등록을 해제한다. 없는 채널이면 거짓을 돌려준다."""
        with self._lock:
            raw = self._read_raw()
            if channel_id not in raw:
                return False
            del raw[channel_id]
            self._write_raw(raw)
            return True

    def _read_raw(self) -> dict[str, Any]:
        """파일 내용을 그대로 읽는다. 알 수 없는 키까지 보존해야 해서 필요하다."""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return dict(raw) if isinstance(raw, Mapping) else {}

    def _write_raw(self, raw: Mapping[str, Any]) -> None:
        """임시 파일에 쓰고 교체한다.

        원본은 대상 파일에 바로 쓴다. 그 동안 읽으면 잘린 JSON 이 읽히고,
        읽는 쪽은 그것을 편집 중 파일로 보고 직전 설정을 유지한다. 교체 방식은
        읽는 쪽이 옛 파일이나 새 파일 하나만 보게 한다.

        쓴 뒤 캐시를 직접 갱신한다. mtime 해상도가 1초인 파일 시스템에서는
        같은 초에 두 번 쓰면 mtime 이 안 바뀌어 캐시가 유지된다.
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
            # 편집 중 저장된 깨진 파일로 전체 처리가 멈추지 않게 한다.
            # mtime 이 또 바뀌므로 고치면 다음 읽기에서 반영된다
            return dict(self._configs)
        if not isinstance(raw, Mapping):
            return {}
        return {
            cid: ChannelConfig.from_dict(cid, data)
            for cid, data in raw.items()
            if isinstance(data, Mapping)
        }
