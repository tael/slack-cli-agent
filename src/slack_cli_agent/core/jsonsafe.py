"""Making a value safe for json.dumps, in one place.

A frozenset leaking into json.dumps dropped a whole request twice: once in the
audit record (sca-kwv) and once in the queued request payload (sca-btw). The
guard lived only in the audit module the second time, so the request path had
none.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

MAX_JSON_DEPTH = 20


def dump_json(payload: Mapping[str, Any]) -> str:
    """Serializes an audit payload, giving up field fidelity before the record.

    Losing one field's shape still leaves a readable record; raising loses the
    whole request's record and, on the caller's path, the request itself
    (sca-kwv). The normal path is a plain dumps, so a well-formed payload pays
    nothing for this.
    """
    try:
        return json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps(json_safe(payload), ensure_ascii=False)


def json_safe(value: Any, depth: int = 0, seen: frozenset[int] = frozenset()) -> Any:
    """Rewrites a value into something json.dumps always accepts.

    A `default=` hook is not enough on its own: it is never called for a
    non-string dict key, and it can raise again on the value it is handed.
    Recursion is bounded by depth and by the ids on the current path, so a
    cycle becomes a placeholder rather than a RecursionError.
    """
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if depth >= MAX_JSON_DEPTH:
        return "<깊이 초과>"
    if id(value) in seen:
        return "<순환 참조>"
    nested = seen | {id(value)}
    if isinstance(value, Mapping):
        return {str(k): json_safe(v, depth + 1, nested) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        # Sorted by the rendered form: the elements themselves may not be
        # mutually comparable, and this only needs a stable order.
        return sorted((json_safe(v, depth + 1, nested) for v in value), key=repr)
    if isinstance(value, (list, tuple)):
        return [json_safe(v, depth + 1, nested) for v in value]
    return str(value)
