"""Records a running process's PID in the state directory.

bot.py:  a single long-lived process wrote one `bot.pid` so an operator (and
the watchdog) could tell a live bot from a dead state directory. This engine
splits that into an ingress and a worker process, so a shared `bot.pid` would
have the two clobber each other; each writes its own `<role>.pid` instead
(StatePaths.pid_file).

The file is removed on a clean exit, but only if it still holds our own PID:
if a newer process has already claimed the slot, deleting the file would erase
the live process's record.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType


class PidFile:
    """Context manager that owns a PID file for the current process."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._pid = os.getpid()

    def __enter__(self) -> PidFile:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(f"{self._pid}\n")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            current = self._path.read_text().strip()
        except FileNotFoundError:
            return
        if current == str(self._pid):
            self._path.unlink(missing_ok=True)
