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


def _merge_pair(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        current = result.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
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


def load_settings_file(path: Path) -> dict[str, Any]:
    """One operator-written settings file, or an empty fragment.

    Absent is normal: these files are operational, and the bot has to boot
    without them. Present but unreadable is not -- ignoring it would run
    the turn with the deny list missing and nothing in the log to say so.
    """
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ConfigError(f"settings 파일을 읽지 못했습니다 : {path} : {e}") from e
    if not isinstance(loaded, dict):
        raise ConfigError(f"settings 파일의 최상위가 객체가 아닙니다 : {path}")
    _check_shape(loaded, path)
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
