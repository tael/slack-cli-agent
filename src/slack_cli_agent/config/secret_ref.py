"""Indirect references for values a profile must not hold itself.

An MCP server's `env` is the one place a profile carries a credential: the
load-time token check exempts that block, so a token written there stays in
the file the web console reads and writes back (sca-dn4). `${env:NAME}` and
`${file:PATH}` let the profile point at the secret instead of holding it.

Resolution happens where the engine command is built, not at load, so the
resolved value never reaches anything that saves the profile back.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from ..core.errors import ConfigError

#: Whole-value only. Partial interpolation would let a typo splice a
#: credential into a URL without anything failing.
_REF = re.compile(r"^\$\{(env|file):(.+)\}$")


def resolve_mapping(
    values: Mapping[str, str], *, where: str, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    environ = os.environ if environ is None else environ
    return {key: _resolve(key, value, where, environ) for key, value in values.items()}


def _resolve(key: str, value: str, where: str, environ: Mapping[str, str]) -> str:
    match = _REF.match(value)
    if match is None:
        return value
    kind, target = match.group(1), match.group(2).strip()
    resolved = _from_env(target, environ) if kind == "env" else _from_file(target)
    if not resolved:
        # An empty credential reaches the MCP server as an auth failure and
        # nothing says where it came from.
        raise ConfigError(
            f"{where} 의 {key} 가 가리키는 {kind} 값이 비었거나 없다 : {target}"
        )
    return resolved


def _from_env(name: str, environ: Mapping[str, str]) -> str:
    return environ.get(name, "").strip()


def _from_file(path: str) -> str:
    try:
        return Path(path).expanduser().read_text(encoding="utf-8").strip()
    except OSError:
        return ""
