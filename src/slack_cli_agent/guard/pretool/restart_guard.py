"""PreToolUse guard: a self-restart command from inside a turn denies (G50).

A bot turn runs inside that turn's sandbox, and restarting the bot process
*from within the turn* deadlocks: the restart waits for the turn's in-flight
request to finish, and that request is the very one that triggered the
restart. reliability/restart.py's module docstring records the two measured
failures this caused in mametchi (sandboxed ``launchctl kickstart`` on
2026-09-22 23:01, a no-op restart leaving 10-hour-old code running on
2026-09-22 22:02). The design rule there is absolute: a turn never restarts
itself; it only writes an apply-request marker for a supervisor process
(started by launchd, outside any turn's sandbox) to act on later.

restart.py gives the engine the *marker* side of that rule. This guard is
the enforcement side mametchi never had as explicit code (gaps.md G50): a
Bash call that would stop, replace, or reload the running bot is denied
before it runs, with the message pointing at the marker path instead.

What denies:
- ``launchctl`` with a mutating subcommand (kickstart / stop / bootout /
  bootstrap / unload / load / remove / kill / disable / enable) aimed at the
  bot's *own* services (RESTART_GUARD_SERVICES), or at a target the guard
  cannot resolve (a shell variable in the service name). Read-only
  ``launchctl list|print|blame|dumpstate`` is left alone -- a turn asking
  "is the worker up?" is fine; a turn kicking it is not.

  2026-11-08: the guard used to deny every mutating launchctl call whatever
  the target. deagent's owner told it to register its own hourly
  autoupdate job (local.deagent.autoupdate); the guard denied
  ``launchctl bootstrap`` of that new job three turns running, and the bot
  handed the command back to the owner each time. Registering or kicking a
  job that is not the running bot cannot deadlock the turn, so only the
  bot's own service labels are denied now. With no service list configured
  the guard keeps the old deny-all behaviour.
- a bot restart/apply script (``apply-restart.sh``, ``restart.sh``, or
  ``apply.sh`` -- mametchi's turn-external restart scripts).
- ``kill``/``pkill``/``killall`` whose target names a configured bot
  process label or the bot's own pid (see RESTART_GUARD_LABELS).

There is deliberately no escape hatch: unlike install_guard's
INSTALL_DESPITE_EXISTING, the deadlock here is not a false positive the
operator can knowingly accept -- the only correct path is the marker, so
the guard always routes there.

Standalone and import-free beyond the standard library, for the same reason
observability/progress_hook.py is. Never fails the tool call -- every path
here ends in exit 0.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

#: Absolute path to this file, for the engine adapter that builds the hook
#: command (same pattern as the other guard/pretool/ modules).
HOOK_SCRIPT = Path(__file__).resolve()

#: launchctl subcommands that change what is running. A turn calling one of
#: these against anything is a restart primitive; the read-only ones below
#: are not matched and pass through untouched.
_LAUNCHCTL_MUTATING = re.compile(
    r"\blaunchctl\s+(kickstart|stop|bootout|bootstrap|unload|load|remove"
    r"|kill|disable|enable)\b"
)

#: mametchi's turn-external restart/apply scripts. Any invocation of one of
#: these from inside a turn is the deadlock the marker exists to dodge.
#: 2026-11-10: the pattern used to match the script name anywhere, so a
#: read such as ``test -e .../apply.sh`` or ``grep x apply.sh`` was denied
#: as a restart. Only a command position counts now: the start of a
#: segment (after ; & | ( or an opening quote, e.g. ``sh -c './apply.sh'``),
#: optionally behind VAR=val assignments and a runner
#: (sudo/nohup/exec/time/env/bash/sh/zsh/source/.).
_RESTART_SCRIPT = re.compile(
    r"""(?:^|[;&|(`'"]|\$\()\s*"""
    r"""(?:\w+=\S*\s+)*"""
    r"""(?:(?:sudo|nohup|exec|time|env(?:\s+\w+=\S*)*|bash|sh|zsh|source|\.)(?:\s+-\S+)*\s+)*"""
    r"""(?:\S*/)?(?:apply-restart|restart|apply)\.sh\b"""
)

#: Process-targeting kill commands. Only deny when the target names a
#: configured bot label (RESTART_GUARD_LABELS) or the bot's own pid --
#: killing an unrelated subprocess is legitimate turn work.
_KILL_CMD = re.compile(r"\b(kill|pkill|killall)\b")

#: Comma/space-separated process labels that identify the bot's own
#: launchd jobs or pidfiles, e.g. "local.guchipachi.worker,guchipachi".
#: Org-specific, so it comes from the environment rather than being
#: hardcoded (the engine is bot-neutral; the labels are not).
_LABELS = [
    lbl for lbl in re.split(r"[,\s]+", os.environ.get("RESTART_GUARD_LABELS", "")) if lbl
]

#: The bot's own launchd service labels (e.g. "local.deagent.worker,
#: local.deagent.ingress"). Mutating launchctl only denies when it names one of
#: these. Empty means unknown, and then every mutating call denies as before.
_SERVICES = [
    s for s in re.split(r"[,\s]+", os.environ.get("RESTART_GUARD_SERVICES", "")) if s
]

#: gui/<uid> domain spellings that are not an unresolved target.
_UID_FORMS = re.compile(r"\$\(\s*id\s+-u\s*\)|\$\{?UID\}?|\$\(\s*id\s+-u\s+-n\s*\)")

#: Path of the apply-request marker to point the model at instead. The
#: supervisor polls this; writing it is how a turn asks for a restart
#: without killing itself. Overridable for a bot's own state convention.
_MARKER_PATH = os.environ.get("RESTART_GUARD_MARKER", "")


def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }, ensure_ascii=False))


#: Marker body: epoch seconds, then this turn's "channel:ts" from TURN_ID, then
#: the change summary. The second line is how the worker knows not to wait on
#: or rerun this turn; the summary goes into the supervisor's completion DM
#: (mametchi apply.sh's required note). The supervisor runs the check, so a
#: turn never tests-and-restarts on its own (H3).
_MARKER_BODY = "printf '%s\\n%s\\n%s\\n' \"$(date +%s)\" \"$TURN_ID\" \"<변경 요약>\""

_NOTE_HINT = (
    " <변경 요약> 1행은 무엇이 달라졌는지 제목이고, 이후 줄은 '- ' 불릿이다."
    " 점검(pytest), 재기동, 실패 시 되돌리기, 결과 DM 은 supervisor 가 한다."
)


def _marker_hint() -> str:
    if _MARKER_PATH:
        return (
            f" 재기동이 필요하면 {_MARKER_BODY} > {_MARKER_PATH} 로"
            " apply-request 마커만 남기고 턴을 끝낸다." + _NOTE_HINT
        )
    return (
        f" 재기동이 필요하면 {_MARKER_BODY} 로 apply-request 마커만 남기고 턴을 끝낸다"
        " (supervisor 가 집는다)." + _NOTE_HINT
    )


def _names_service(segment: str, label: str) -> bool:
    """Whether *segment* targets *label* itself (not a longer sibling label).

    ``local.deagent.worker`` matches ``gui/501/local.deagent.worker`` and
    ``.../local.deagent.worker.plist`` but not ``local.deagent.worker2``.
    """
    pat = re.escape(label) + r"(?:\.plist)?(?![\w.-])"
    return re.search(pat, segment) is not None


def _launchctl_targets_self(cmd: str) -> bool:
    """Whether a mutating launchctl call in *cmd* could hit the running bot."""
    for m in _LAUNCHCTL_MUTATING.finditer(cmd):
        if not _SERVICES:
            return True
        # The rest of this shell command, up to the next separator.
        segment = re.split(r"&&|\|\||;|\||\n", cmd[m.end():], maxsplit=1)[0]
        if any(_names_service(segment, lbl) for lbl in _SERVICES):
            return True
        if "$" in _UID_FORMS.sub("", segment):
            return True
    return False


_OTHER_JOBS_OK = (
    " 봇 자신의 서비스(" + ", ".join(_SERVICES) + ")가 아닌 다른 launchd 작업은 "
    "등록, 기동, 해제를 턴 안에서 해도 된다. 대상을 라벨이나 plist 경로로 직접 적는다."
) if _SERVICES else ""


def _self_restart_reason(cmd: str) -> str | None:
    """The deny reason if *cmd* would restart the running bot, else None."""
    if _launchctl_targets_self(cmd):
        return (
            "봇 턴 안에서 launchctl 로 봇 프로세스를 멈추거나 되살리면 교착된다: 재기동은 "
            "지금 처리 중인 바로 그 요청이 끝나길 기다리는데, 그 요청이 이 턴이다."
            + _marker_hint() + _OTHER_JOBS_OK
        )
    if _RESTART_SCRIPT.search(cmd):
        return (
            "봇 턴 안에서 재기동·적용 스크립트를 직접 돌리면 교착된다 "
            "(reliability/restart.py 참고)." + _marker_hint()
        )
    if _KILL_CMD.search(cmd) and _LABELS:
        low = cmd.lower()
        hit = [lbl for lbl in _LABELS if lbl.lower() in low]
        if hit:
            return (
                "이 kill 명령은 봇 자신(" + ", ".join(hit) + ")을 겨냥한다. 턴 안에서 "
                "봇을 죽이면 재기동이 교착된다." + _marker_hint()
            )
    return None


def main(argv: list[str]) -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 - never fail the tool call
        return 0
    try:
        if data.get("tool_name") != "Bash":
            return 0
        cmd = (data.get("tool_input") or {}).get("command") or ""
        reason = _self_restart_reason(str(cmd))
        if reason:
            deny(reason)
    except Exception:  # noqa: BLE001 - never fail the tool call
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
