"""Fake executables for tests.

macOS assesses every newly created executable on its first exec, one at a time
system-wide (measured 2026-11-17: 24 new scripts in parallel 2.13s, 24 symlinks to
an already-run script 0.01s). Fresh fakes per test made xdist workers queue on
that check. Each fake is therefore a symlink to one pre-run launcher that execs the
fake's body file with /bin/bash, so pid and signal handling belong to the body.
"""

from __future__ import annotations

import atexit
import shutil
import subprocess
import tempfile
from pathlib import Path

BODY_SUFFIX = ".fakebody"

# Only bash parameter expansion: tests may narrow PATH.
_LAUNCHER = f'#!/bin/bash\nexec /bin/bash "$0{BODY_SUFFIX}" "$@"\n'

_launcher: Path | None = None


def _shared_launcher() -> Path:
    global _launcher
    if _launcher is None:
        directory = Path(tempfile.mkdtemp(prefix="fake-exec-"))
        atexit.register(shutil.rmtree, directory, True)
        path = directory / "launcher"
        path.write_text(_LAUNCHER, encoding="utf-8")
        path.chmod(0o755)
        Path(f"{path}{BODY_SUFFIX}").write_text("exit 0\n", encoding="utf-8")
        subprocess.run([str(path)], check=True, timeout=30)
        # Read-only so a test writing through a fake's symlink fails instead of
        # rewriting every fake in this worker.
        path.chmod(0o555)
        _launcher = path
    return _launcher


def write_fake(path: Path, body: str) -> Path:
    """Place a fake executable at `path` running the bash `body` (shebang optional).

    Rewriting an existing fake replaces only its body; writing through the symlink
    would overwrite the shared launcher.
    """
    if body.startswith("#!"):
        body = body.partition("\n")[2]
    Path(f"{path}{BODY_SUFFIX}").write_text(body, encoding="utf-8")
    if path.is_symlink() or path.exists():
        path.unlink()
    path.symlink_to(_shared_launcher())
    return path
