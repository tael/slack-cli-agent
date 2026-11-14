"""PreToolUse guard: an install command without a prior installed-check denies.

Ports mametchi's install_guard_hook.py (read-only reference at
~/.mametchi/install_guard_hook.py) as an engine built-in: a Bash call that
installs something (brew/cargo/go/pipx/npm -g/pip/uv tool/a release
download) is denied unless the same session already ran an
``installed-check <name>`` naming it. A name installed-check already
reported as found stays denied even after the check, so a result that was
never read does not open the door either -- the only way past that is
prefixing the command with ``INSTALL_DESPITE_EXISTING=<reason>``.

The judgment logic (patterns, exemptions, the two-stage check-then-found
gate) is an unmodified port. Differences from the original:
- The session marker directory drops the "mametchi" name (any bot built on
  this engine shares the one guard), and both it and the installed-check
  result directory are overridable via env for a test or an alternate
  state convention, rather than hardcoded under ~/.
- Project-dependency installs (pip install -r/-e, a venv's own pip, uv
  sync) are exempt in the original and remain exempt here unchanged.

Standalone and import-free beyond the standard library, for the same
reason observability/progress_hook.py is. Never fails the tool call --
every path here ends in exit 0.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

#: Absolute path to this file, for the engine adapter that builds the hook
#: command (same pattern as observability/progress_hook.py's HOOK_SCRIPT).
HOOK_SCRIPT = Path(__file__).resolve()

MARK_DIR = Path(os.environ.get("INSTALL_GUARD_MARK_DIR", "/tmp/slack-cli-agent-installcheck"))
RESULT_DIR = Path(
    os.environ.get("INSTALL_GUARD_RESULT_DIR", str(Path.home() / ".cache" / "installed-check"))
)

INSTALL_PATTERNS = [
    r"\bbrew\s+(install|reinstall|upgrade)\b",
    r"\b(cargo|go)\s+install\b",
    r"\bpipx\s+install\b",
    r"\bgh\s+api\b[^|;&]*releases[^|;&]*>",
    r"\bgh\s+release\s+download\b",
    r"\bnpm\s+(install|i)\b[^|;&]*\s(-g|--global)\b",
    r"\b(pip3?|uv\s+pip)\s+install\b",
    r"\buv\s+tool\s+install\b",
    # Data downloads are not an install -- only release builds/installers.
    r"\b(curl|wget)\b[^|;&]*(releases/download|/archive/|\.dmg\b|\.pkg\b)",
]

# Project dependency installs. Flagging these would just be a false
# positive, not a tool install.
EXEMPT_PATTERNS = [
    r"\b(pip3?|uv\s+pip)\s+install\s+(-r|--requirement|-e|--editable)\b",
    r"/\.?venv[^\s]*/bin/pip3?\s+install\b",
]


def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }, ensure_ascii=False))


def main(argv: list[str]) -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 - never fail the tool call
        return 0
    try:
        if data.get("tool_name") != "Bash":
            return 0
        cmd = (data.get("tool_input") or {}).get("command") or ""
        session = re.sub(r"[^A-Za-z0-9_-]", "_", str(data.get("session_id") or "nosession"))
        sess_dir = MARK_DIR / session

        # Only an invocation at the start of the command counts. A string
        # like "echo installed-check" would not leave a mark here.
        checked_now = re.match(r"\s*(\S*/)?installed-check\s+([^|;&>]+)", cmd)
        if checked_now:
            sess_dir.mkdir(parents=True, exist_ok=True)
            for name in checked_now.group(2).split():
                (sess_dir / name.lower().replace("/", "_")).touch()
            return 0

        if not any(re.search(p, cmd) for p in INSTALL_PATTERNS):
            return 0
        if any(re.search(p, cmd) for p in EXEMPT_PATTERNS):
            return 0

        low = cmd.lower()
        checked = [p.name for p in sess_dir.iterdir()] if sess_dir.is_dir() else []
        hit = [n for n in checked if n in low]
        if not hit:
            deny(
                "설치·내려받기 명령 전에 그 도구가 이미 있는지 확인해야 한다. "
                "이 명령에 들어 있는 도구 이름으로 installed-check <이름> 을 먼저 실행한다. "
                "다른 이름으로 돌린 확인은 인정하지 않는다."
            )
            return 0

        if "INSTALL_DESPITE_EXISTING=" in cmd:
            return 0
        found = []
        for name in hit:
            result = RESULT_DIR / name
            if result.exists() and result.read_text(encoding="utf-8").startswith("found"):
                found.append(
                    f"{name} : " + " / ".join(result.read_text(encoding="utf-8").splitlines()[1:6])
                )
        if found:
            deny(
                "installed-check 가 이미 설치돼 있다고 판정한 도구다. 새로 설치하지 말고 기존 설치본을 쓴다. "
                + " | ".join(found)
                + " | 기존 것을 알고도 다시 설치해야 하면 명령 앞에 "
                "INSTALL_DESPITE_EXISTING=<사유> 를 붙이고 사유를 보고에 쓴다."
            )
    except Exception:  # noqa: BLE001 - never fail the tool call
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
