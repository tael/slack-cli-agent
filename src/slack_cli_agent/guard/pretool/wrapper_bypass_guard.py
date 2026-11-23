"""PreToolUse guard: calling a binary by absolute path when PATH holds a wrapper for it denies.

2026-11-23 private-taeil. The bot's ssh/scp/sftp reached MB-peteam only through
PATH wrappers that add the bot's own ssh config. ``/usr/bin/ssh host`` skips the
wrapper and is refused again (Permission denied), so the fix silently stops
holding the moment a turn types the absolute path.

Generic rule, no host or bot name hardcoded: if a Bash command invokes
``/usr/bin/X`` (or /bin, /usr/sbin, /sbin) and an earlier PATH directory holds
a script named X that itself delegates to that same absolute path, the call
bypasses a deliberate wrapper and is denied. Prefix the command with
``WRAPPER_BYPASS=<reason>`` to pass anyway.

Standard library only; never fails the tool call -- every path ends in exit 0.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

HOOK_SCRIPT = Path(__file__).resolve()

_ABS_CALL_RE = re.compile(r"(?:^|[\s;&|(`$])(/(?:usr/)?s?bin/([A-Za-z0-9._+-]+))(?=\s|$|[;&|)`])")
_ESCAPE_RE = re.compile(r"\bWRAPPER_BYPASS=\S")


def wrapper_for(abs_path: str, name: str, path_env: str) -> str | None:
    """Return the wrapper script that shadows abs_path on PATH, or None."""
    abs_dir = os.path.dirname(abs_path)
    for d in path_env.split(os.pathsep):
        if not d:
            continue
        if os.path.realpath(d) == os.path.realpath(abs_dir):
            return None  # the real binary comes first; nothing to bypass
        cand = Path(d) / name
        try:
            if not (cand.is_file() and os.access(cand, os.X_OK)):
                continue
            head = cand.read_bytes()[:8192]
        except OSError:
            continue
        if head.startswith(b"#!") and abs_path.encode() in head:
            return str(cand)
        return None  # a different executable shadows it -- not a wrapper of this binary
    return None


def find_bypass(command: str, path_env: str) -> tuple[str, str] | None:
    if _ESCAPE_RE.search(command):
        return None
    for m in _ABS_CALL_RE.finditer(command):
        wrapper = wrapper_for(m.group(1), m.group(2), path_env)
        if wrapper:
            return m.group(1), wrapper
    return None


def main() -> int:
    try:
        data = json.load(sys.stdin)
        if data.get("tool_name") != "Bash":
            return 0
        command = str((data.get("tool_input") or {}).get("command", ""))
        hit = find_bypass(command, os.environ.get("PATH", ""))
        if not hit:
            return 0
    except Exception:  # noqa: BLE001 - never fail the tool call
        return 0
    abs_path, wrapper = hit
    name = os.path.basename(abs_path)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"{abs_path} 를 절대경로로 불렀다. PATH 앞쪽의 {wrapper} 가 이 명령을 감싸 설정을 붙이는데, "
                f"절대경로는 그 래퍼를 건너뛰어 래퍼가 막던 실패가 다시 난다. 경로 없이 {name} 로 부른다. "
                "래퍼 없는 동작을 일부러 재현하는 것이면 명령 앞에 WRAPPER_BYPASS=<사유> 를 붙인다."
            ),
        },
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
