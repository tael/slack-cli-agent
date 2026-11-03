"""PreToolUse guard: deny or rewrite a Bash scan that would touch all of
disk, all of the home directory, or a TCC-protected path under ~/Library.

Ports mametchi's scan_guard_hook.py (read-only reference at
~/.mametchi/scan_guard_hook.py) as an engine built-in. A recursive search
(find/grep -r/rg/fd/du/ls -R/tree/...) rooted at /, /Users, the home
directory, or an unsafe path under ~/Library makes macOS pop a "wants to
access data from other apps" permission dialog that blocks the whole
process until a human clicks it -- fatal for a bot turn with nobody at the
keyboard. Rather than deny every such find outright, a find whose risky
roots can be excluded with `-prune` is rewritten to add the exclusion and
still runs; only a find wrapped in sudo/xargs/sh -c, or one using `-delete`
/`-d`/`-depth` (post-order traversal that -prune can't steer), is denied
outright. Non-find scanners (grep -r, rg, fd, du, ls -R, tree, ...) and any
command module directly opening a protected path (ls, cat, stat, cp,
sqlite3, ...) are denied rather than rewritten.

The rewrite/risk logic (`risk`, `protected`, `check_segment`,
`rewrite_find`, segment splitting, heredoc stripping, the TCC-safe
~/Library subdirectories) is an unmodified port. Differences from the
original:
- The rejection log path is opt-in (argv[1] or the SCAN_GUARD_LOG env var)
  instead of a hardcoded ~/.mametchi/logs path, silently skipped when
  neither is given -- same reasoning as turn_budget.py's TURN_BUDGET_LOG.
- The deny message no longer points at mametchi's own instruction files
  (AGENTS.md, _도구지침.md); it just names the candidate directories to
  retry under, which is a profile- and organization-neutral hint.
- The "where this file actually lives, registered" side-channel
  (`registered_hits`) still shells out to an `installed-check`-shaped tool
  if one is configured (SCAN_GUARD_INSTALLED_CHECK_BIN env, defaulting to
  the original's ~/.local/bin/installed-check); any failure there
  (including the binary not existing) is swallowed exactly like the
  original, so a deployment without that tool just gets no extra hint.

Standalone and import-free beyond the standard library, for the same
reason observability/progress_hook.py is. Never fails the tool call --
every path here ends in exit 0.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

#: Absolute path to this file, for the engine adapter that builds the hook
#: command (same pattern as observability/progress_hook.py's HOOK_SCRIPT).
HOOK_SCRIPT = Path(__file__).resolve()

HOME = os.path.expanduser("~")
SCANNERS = {"find", "grep", "egrep", "rg", "fd", "du", "ls", "tree", "ag", "gfind"}
# Subdirectories under ~/Library that are not TCC-protected. A scan rooted
# only here passes.
LIB_SAFE = ("LaunchAgents", "Logs", "Caches", "Preferences", "Python", "pnpm", "Fonts")

# Home subdirectories macOS treats as a separate TCC service, so opening one
# directly (by any command, not just a recursive scan) can pop a dialog.
# Downloads is left out -- a person pointing the bot at a file they sent is
# legitimate, and it's not one of the directories macOS gates.
TCC_HOME_DIRS = ("Desktop", "Documents", "Pictures", "Movies", "Music", ".Trash")


def _log_path(argv: list[str]) -> Path | None:
    if len(argv) >= 2 and argv[1]:
        return Path(argv[1])
    env_path = os.environ.get("SCAN_GUARD_LOG")
    return Path(env_path) if env_path else None


def norm(path: str) -> str:
    path = path.strip().strip("'\"")
    path = path.replace("$HOME", HOME).replace("${HOME}", HOME)
    if path == "~" or path.startswith("~/"):
        path = HOME + path[1:]
    if path.startswith("/"):
        # /Users/Shared/../taeil.kim and /System/Volumes/Data/Users/... are
        # aliases for the same place.
        path = os.path.normpath(path)
        if path.startswith("/System/Volumes/Data/"):
            path = path[len("/System/Volumes/Data"):]
    return path.rstrip("/") or "/"


def protected(path: str) -> bool:
    """True when any command opening this path directly can pop a dialog."""
    p = norm(path)
    for d in TCC_HOME_DIRS:
        if p == f"{HOME}/{d}" or p.startswith(f"{HOME}/{d}/"):
            return True
    r = risk(p)
    return bool(r and r[0] == "library" and r[1] < 0)


def risk(path: str) -> tuple[str, int] | None:
    """(kind, allowed maxdepth) when `path` is a risky scan root, else None."""
    p = norm(path)
    for d in TCC_HOME_DIRS:
        if p == f"{HOME}/{d}" or p.startswith(f"{HOME}/{d}/"):
            return ("library", -1)
    if p in ("/", "/System/Volumes/Data"):
        return ("root", 2)
    if p == "/Users":
        return ("users", 1)
    if p == HOME:
        return ("home", 1)
    lib = HOME + "/Library"
    if p == lib:
        return ("library", 0)
    if p.startswith(lib + "/"):
        sub = p[len(lib) + 1:].split("/")[0]
        if sub in LIB_SAFE:
            return None
        return ("library", -1)
    return None


def split_segments(cmd: str) -> list[str]:
    # A find's own `-exec ... \;` terminator is not a shell separator.
    return [s for s in re.split(r"\|\||&&|(?<!\\)[;|&\n]|\$\(|`", cmd) if s.strip()]


def spaced(seg: str) -> str:
    # Pulls a glued-on `(`/`)` apart into its own token.
    return re.sub(r"\)(?=\s|$)", " )", re.sub(r"(^|\s)\(", r"\1( ", seg))


def inner_shell_cmds(cmd: str) -> list[str]:
    """Commands inside `sh -c '...'`. Quoted, so not rewritten -- denied
    outright if risky."""
    return [m.group(2) for m in re.finditer(r"\b(?:sh|bash|zsh)\s+-l?c\s+(['\"])(.*?)\1", cmd, re.DOTALL)]


def check_segment(seg: str) -> str | None:
    try:
        toks = shlex.split(spaced(seg), posix=True)
    except ValueError:
        toks = seg.split()
    i = 0
    while i < len(toks) and (
        re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[i])
        or toks[i] in (
            "sudo", "xargs", "command", "nice", "time", "timeout", "gtimeout",
            "(", "{", "!", "then", "do", "else", "exec", "sh", "bash", "zsh", "-c",
        )
    ):
        i += 1
        if i < len(toks) and toks[i - 1] in ("timeout", "gtimeout") and re.match(r"^\d", toks[i]):
            i += 1
    if i >= len(toks):
        return None
    # Even a non-recursive command (ls, cat, stat, cp, sqlite3, ...) can pop
    # a dialog if it opens a protected path directly.
    prev_tok = ""
    for t in toks[i + 1:]:
        skip = prev_tok in (
            "-path", "-ipath", "-wholename", "-not", "!", "--exclude-dir",
            "--exclude", "-g", "--glob", "--ignore",
        )
        prev_tok = t
        if skip or t.startswith(("--exclude", "--glob=!", "-g!")):
            continue
        for piece in re.split(r"[=:]", t):
            if piece.startswith(("/", "~", "$HOME", "${HOME}")) and protected(piece):
                return f"{os.path.basename(toks[i])} 가 보호 경로({norm(piece)})를 직접 연다"
    prog = os.path.basename(toks[i])
    if prog not in SCANNERS:
        return None
    args = toks[i + 1:]
    joined = " ".join(args)
    recursive = True
    if prog in ("grep", "egrep"):
        recursive = any(
            re.match(r"^-[A-Za-z]*[rR]", a) or a in ("--recursive", "--dereference-recursive")
            for a in args
        )
    if prog == "ls":
        recursive = any(re.match(r"^-[A-Za-z]*R", a) for a in args)
    if not recursive:
        return None
    maxdepth = None
    m = re.search(r"-(?:max-?depth|d)\s+(\d+)", joined) if prog in ("find", "gfind", "fd", "du", "tree") else None
    if prog == "tree":
        m = re.search(r"-L\s+(\d+)", joined) or m
    if m:
        maxdepth = int(m.group(1))
    excludes_lib = bool(re.search(r"Library", joined) and re.search(r"-prune|--exclude|-g\s*!|--glob\s*!|-E\s", joined))
    prev = ""
    for a in args:
        if prev in (
            "-path", "-ipath", "-wholename", "-name", "-iname", "-regex",
            "--exclude-dir", "--exclude", "-g", "--glob", "-E", "--ignore",
        ):
            prev = a
            continue
        prev = a
        if a.startswith("-") and a != "/":
            continue
        r = risk(a)
        if not r:
            continue
        kind, allowed = r
        if kind == "library" and allowed < 0:
            return f"{prog} 가 ~/Library 의 보호 경로({norm(a)})를 훑는다"
        if maxdepth is not None and allowed >= 0 and maxdepth <= allowed:
            continue
        if kind == "home" and excludes_lib:
            continue
        return f"{prog} 가 {norm(a)} 전체를 훑는다"
    return None


# A find rooted at a risky path is rewritten rather than denied: the risky
# roots are excluded with -prune and the rest runs. -delete/-d/-depth are
# post-order, so -prune can't steer them -- denied outright, same as a find
# wrapped in sudo/xargs/sh -c (not rewritten, since the wrapper hides what
# actually ran).
PRUNE_UNDER_HOME = ("Library", "Desktop", "Documents", "Downloads", "Pictures", "Movies", "Music", ".Trash")
PRUNE_ABS = ("/System/Volumes/Data", "/Volumes")
FIND_PRE_OPTS = {"-E", "-H", "-L", "-P", "-X", "-d", "-s", "-x", "-f"}
FIND_ACTIONS = {
    "-print", "-print0", "-exec", "-execdir", "-ok", "-okdir", "-ls", "-fprint",
    "-fls", "-fprint0", "-printf", "-delete",
}
WRAPPERS = ("sudo", "xargs", "sh", "bash", "zsh", "-c")


def rewrite_find(seg: str, cwd: str) -> tuple[str, list[str]] | tuple[str, str] | None:
    """(new command, excluded roots) if find was rewritten, ("DENY", reason)
    if it must be denied outright, None if this segment isn't a risky find."""
    try:
        toks = shlex.split(spaced(seg), posix=True)
    except ValueError:
        return None
    i = 0
    wrapped = False
    pre_toks = (
        "command", "nice", "time", "timeout", "gtimeout", "(", "{", "!", "then", "do", "else", "exec",
        *WRAPPERS,
    )
    while i < len(toks) and (
        re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[i]) or toks[i] in pre_toks
    ):
        if toks[i] in WRAPPERS:
            wrapped = True
        i += 1
        if i < len(toks) and toks[i - 1] in ("timeout", "gtimeout") and re.match(r"^\d", toks[i]):
            i += 1
    if i >= len(toks) or os.path.basename(toks[i]) not in ("find", "gfind"):
        return None
    prefix, prog = toks[:i], toks[i]
    j = i + 1
    pre = []
    while j < len(toks) and toks[j] in FIND_PRE_OPTS:
        pre.append(toks[j])
        j += 1
        if pre[-1] == "-f" and j < len(toks):
            pre.append(toks[j])
            j += 1
    paths = []
    while j < len(toks) and not (toks[j].startswith("-") or toks[j] in ("(", "!", ")")):
        paths.append(toks[j])
        j += 1
    expr = toks[j:]
    tail: list[str] = []
    opened = sum(1 for t in toks[:i] if t in ("(", "{"))
    while expr and (re.match(r"^\d?>{1,2}|^<", expr[-1]) or (opened and expr[-1] in (")", "}"))):
        if expr[-1] in (")", "}"):
            opened -= 1
        tail.insert(0, expr.pop())
    resolved = []
    hit = False
    md = re.search(r"-maxdepth\s+(\d+)", " ".join(expr))
    for pth in paths:
        n = norm(pth)
        if not n.startswith("/"):
            n = os.path.normpath(os.path.join(cwd or os.getcwd(), n))
        r = risk(n)
        if r and r[0] == "library" and r[1] < 0:
            return ("DENY", f"find 가 ~/Library 의 보호 경로({n})를 직접 훑는다. 이 경로는 사람에게 허용 창을 띄운다")
        if r and r[0] in ("root", "users", "home") and not (md and int(md.group(1)) <= r[1]):
            hit = True
        resolved.append(n)
    if not hit:
        return None
    if wrapped:
        return (
            "DENY",
            ("sudo, xargs, sh -c 로 감싼 find 가 전체를 훑는다. "
             "감싸지 말고 find 를 직접 부르면 보호 경로를 빼고 실행된다"),
        )
    if "-delete" in expr or "-d" in pre or "-depth" in expr:
        return (
            "DENY",
            "-delete, -d, -depth 는 후위 순회라 보호 경로를 뺄 수 없다. 대상 디렉터리를 좁혀 다시 부른다",
        )
    excl = [f"{HOME}/{d}" for d in PRUNE_UNDER_HOME] + list(PRUNE_ABS)
    prune = ["("]
    for k, e in enumerate(excl):
        if k:
            prune.append("-o")
        prune += ["-path", e]
    prune += [")", "-prune", "-o"]
    has_action = any(t in FIND_ACTIONS for t in expr)
    body = (["(", *expr, ")"]) if expr else []
    if not has_action:
        body += ["-print"]
    find_call = shlex.join([prog, *pre, *resolved, *prune, *body])
    new = " ".join([*prefix, find_call, *tail])
    return (new, excl)


def registered_hits(cmd: str) -> str:
    """Best-effort extra hint: where an installed-check-shaped lookup says a
    searched-for name already lives, read before waiting on the rewritten
    find. Silent no-op if the configured tool is missing or errors."""
    bin_path = os.environ.get(
        "SCAN_GUARD_INSTALLED_CHECK_BIN", str(Path(HOME) / ".local" / "bin" / "installed-check")
    )
    names = re.findall(r"-i?name\s+['\"]?([^'\"\s]+)", cmd)
    stems = []
    for n in names:
        stem = re.sub(r"\.[A-Za-z0-9]+$", "", n.replace("*", "").replace("?", ""))
        if len(stem) >= 3 and stem not in stems:
            stems.append(stem)
    out = []
    for stem in stems[:2]:
        try:
            result = subprocess.run(
                [bin_path, stem], capture_output=True, text=True, timeout=6, stdin=subprocess.DEVNULL,
            )
        except Exception:  # noqa: BLE001 - a missing/failing tool is a no-op, not a guard failure
            continue
        section = ""
        for line in result.stdout.splitlines():
            if line.startswith("-- "):
                section = line[3:]
                continue
            if line.startswith(("==", ">>")) or not line.strip():
                continue
            out.append(f"- [{section.split(' :')[0]}] {line.strip()[:220]}")
    return "\n".join(out[:12])


def _write_log(log_path: Path | None, rec: dict[str, object]) -> None:
    if log_path is None:
        return
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def deny(reason: str, data: dict[str, Any], log_path: Path | None) -> None:
    _write_log(log_path, {
        "session": data.get("session_id"), "tool": data.get("tool_name"),
        "input": data.get("tool_input"), "reason": reason,
    })
    msg = (
        f"거부 : {reason}. 디스크 전체, 홈 전체, ~/Library 를 훑으면 macOS 가 '다른 앱의 데이터에 접근' 창을 띄우고 "
        "사람이 누를 때까지 봇이 멈춘다. 후보 디렉터리를 지정해 다시 찾는다. "
        "홈을 꼭 훑어야 하면 find ~ -path ~/Library -prune -o -name <이름> -print 형태로 Library 를 뺀다."
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": msg,
        },
    }, ensure_ascii=False))


def main(argv: list[str]) -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 - never fail the tool call
        return 0
    try:
        log_path = _log_path(argv)
        tool = data.get("tool_name")
        tool_input = data.get("tool_input") or {}
        if tool == "Bash":
            cmd = tool_input.get("command") or ""
            new_cmd, excluded = cmd, None
            # A heredoc body is file content, not a command -- its text
            # must not be judged as if it were about to run.
            cmd = re.sub(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\s*(?=\n|$)", "<<HEREDOC", cmd, flags=re.DOTALL)
            for inner in inner_shell_cmds(cmd):
                for seg in split_segments(inner):
                    if rewrite_find(seg, data.get("cwd") or "") or check_segment(seg):
                        deny(
                            "sh -c 로 감싼 명령이 전체를 훑는다. "
                            "감싸지 말고 find 를 직접 부르면 보호 경로를 빼고 실행된다",
                            data, log_path,
                        )
                        return 0
            for seg in split_segments(cmd):
                rw = rewrite_find(seg, data.get("cwd") or "")
                if rw and rw[0] == "DENY":
                    deny(str(rw[1]), data, log_path)
                    return 0
                if rw:
                    new_cmd = new_cmd.replace(seg.strip(), rw[0], 1)
                    excluded = rw[1]
                    continue
                reason = check_segment(seg)
                if reason:
                    deny(reason, data, log_path)
                    return 0
            if excluded:
                note = (
                    "find 가 macOS 허용 창을 띄우는 경로를 자동으로 뺐다 : " + ", ".join(excluded)
                    + ". 이 경로 안의 결과는 없는 것이 아니라 조회하지 않은 것이다. 답에서 없다고 쓰지 않는다"
                )
                reg = registered_hits(cmd)
                if reg:
                    note += (
                        "\n등록 설정에서 먼저 찾은 위치 (installed-check). find 결과를 기다리기 전에 이것부터 본다. "
                        "실행 중인 것은 등록 설정의 경로이고, find 가 찾는 다른 사본은 실행되지 않는 것일 수 있다 :\n"
                        + reg
                    )
                _write_log(log_path, {
                    "session": data.get("session_id"), "tool": "Bash", "rewrite": True,
                    "from": cmd, "to": new_cmd,
                })
                updated = dict(tool_input)
                updated["command"] = new_cmd
                print(json.dumps({
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "updatedInput": updated,
                        "additionalContext": note,
                    },
                }, ensure_ascii=False))
        elif tool in ("Grep", "Glob"):
            path = tool_input.get("path")
            if path:
                r = risk(path)
                if r:
                    deny(f"{tool} 도구가 {norm(path)} 전체를 훑는다", data, log_path)
        elif tool in ("Read", "Edit", "Write", "NotebookEdit"):
            # A file tool reading ~/Library/Messages/chat.db directly can
            # also pop the dialog -- not just a recursive scan.
            path = tool_input.get("file_path") or tool_input.get("notebook_path")
            if path and protected(path):
                deny(f"{tool} 도구가 보호 경로({norm(path)})를 연다", data, log_path)
    except Exception:  # noqa: BLE001 - never fail the tool call
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
