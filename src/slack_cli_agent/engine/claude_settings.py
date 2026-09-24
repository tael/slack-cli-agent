"""Building the one value claude's --settings takes.

That flag accepts a single file or a single JSON string (claude 2.1.263),
but three things have to reach it: the bot's own settings, the overlay for
this request's trust level, and the progress hook, whose log path differs
per request. So the merge happens here rather than by handing claude
several sources.

Merge shape follows what claude does across its own settings levels --
lists join instead of replacing, and hook entries merge rather than one
level dropping another's.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.errors import ConfigError


def merge_settings(*fragments: Mapping[str, Any]) -> dict[str, Any]:
    """The fragments as one settings document, left to right.

    Dicts merge by key, lists join and drop repeats, anything else takes
    the later value. Mismatched kinds take the later value too: a badly
    written operator file should not raise from inside a merge.
    """
    merged: dict[str, Any] = {}
    for fragment in fragments:
        merged = _merge_pair(merged, fragment)
    return merged


#: Keys claude replaces rather than joins across settings levels. Joining a
#: narrowed model list would leave the wider one from the base file in it.
_REPLACE_KEYS = ("fallbackModel", "modelPicker", "availableModels", "modelSettings")


def _merge_pair(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        current = result.get(key)
        if key in _REPLACE_KEYS:
            result[key] = value
        elif isinstance(current, Mapping) and isinstance(value, Mapping):
            result[key] = _merge_pair(current, value)
        elif isinstance(current, list) and isinstance(value, list):
            result[key] = _join(current, value)
        else:
            result[key] = value
    return result


def _join(base: list[Any], overlay: list[Any]) -> list[Any]:
    """Both lists, repeats dropped. Entries may be dicts, so equality
    decides rather than a set."""
    joined = list(base)
    for item in overlay:
        if item not in joined:
            joined.append(item)
    return joined


@dataclass(frozen=True)
class _Seen:
    """What one path held the last time this process read it."""

    present: bool
    denies: bool


#: Per path, the last reading. The boot check reads every settings path
#: (preflight/checks.py EngineSettingsCheck), so the first entries are the
#: boot-time picture without a separate snapshot call; a process that skips
#: preflight still gets its baseline from the first request.
_LEDGER: dict[Path, _Seen] = {}


def reset_settings_ledger() -> None:
    """Forget the readings so far. A restart makes a new baseline."""
    _LEDGER.clear()


def load_settings_file(path: Path) -> dict[str, Any]:
    """One operator-written settings file, or an empty fragment.

    Absent is normal: these files are operational, and the bot has to boot
    without them. Present but unreadable is not -- ignoring it would run
    the turn with the deny list missing and nothing in the log to say so.

    Absent *after* this process already read it is not normal either. The
    boot check only looks once, so a file removed afterwards would leave
    every later turn running with no deny and nothing in the log (sca-1aji).
    A deny list that was there and is now empty ends the same way, so both
    stop the turn rather than run it quietly. Restarting clears the
    baseline, which is how an operator retires a deny list on purpose.
    """
    seen = _LEDGER.get(path)
    if not path.exists():
        if seen is not None and seen.present:
            raise ConfigError(
                f"기동 때 읽은 settings 파일이 사라졌습니다 : {path}"
                " - deny 목록 없이 실행하지 않습니다"
            )
        _LEDGER[path] = _Seen(present=False, denies=False)
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ConfigError(f"settings 파일을 읽지 못했습니다 : {path} : {e}") from e
    if not isinstance(loaded, dict):
        raise ConfigError(f"settings 파일의 최상위가 객체가 아닙니다 : {path}")
    _check_shape(loaded, path)
    denies = bool((loaded.get("permissions") or {}).get("deny"))
    if seen is not None and seen.denies and not denies:
        raise ConfigError(
            f"기동 때 읽은 settings 의 deny 목록이 비었습니다 : {path}"
            " - deny 목록 없이 실행하지 않습니다"
        )
    _LEDGER[path] = _Seen(present=True, denies=denies)
    return loaded


#: Keys whose kind the merge depends on. A list where a dict belongs makes
#: the merge take the later value wholesale, so an overlay written as
#: {"permissions": []} would drop the base file's deny list without a word.
#: Only the keys we merge are listed; claude warns about the rest itself.
_DICT_KEYS = ("permissions", "hooks", "env")
_LIST_KEYS = ("allow", "ask", "deny")


def _check_shape(loaded: Mapping[str, Any], path: Path) -> None:
    # A written-out null is a wrong kind, not an absent key: the merge takes
    # the later value, so an overlay saying {"permissions": null} drops the
    # base file's deny list. Absence is the only way to say nothing here.
    for key in _DICT_KEYS:
        if key in loaded and not isinstance(loaded[key], dict):
            raise ConfigError(f"settings 의 {key} 가 객체가 아닙니다 : {path}")
    permissions = loaded.get("permissions") or {}
    for key in _LIST_KEYS:
        if key in permissions and not isinstance(permissions[key], list):
            raise ConfigError(f"settings 의 permissions.{key} 가 목록이 아닙니다 : {path}")
    for event, groups in (loaded.get("hooks") or {}).items():
        if not isinstance(groups, list):
            raise ConfigError(f"settings 의 hooks.{event} 가 목록이 아닙니다 : {path}")
