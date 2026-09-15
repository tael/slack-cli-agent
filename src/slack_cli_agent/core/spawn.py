"""Runs a piece of work off the caller's thread.

Emoji reviews call the engine, which takes minutes. The original bot.py
ran them in a daemon thread so the Slack event handler returned at once;
this package called them inline and blocked every other event for the
whole review (6 minutes, measured 2026-09-15).

Tests take InlineTaskSpawner so the work is done when spawn() returns.
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable

log = logging.getLogger(__name__)


class TaskSpawner(ABC):
    @abstractmethod
    def spawn(self, name: str, work: Callable[[], None]) -> None: ...

    @staticmethod
    def _guarded(name: str, work: Callable[[], None]) -> Callable[[], None]:
        """Keeps a failure from reaching the caller, which in the inline
        case is the Slack event handler."""
        def run() -> None:
            try:
                work()
            except Exception:
                log.exception("작업 실패: %s", name)
        return run


class ThreadTaskSpawner(TaskSpawner):
    def spawn(self, name: str, work: Callable[[], None]) -> None:
        threading.Thread(target=self._guarded(name, work), name=name, daemon=True).start()


class InlineTaskSpawner(TaskSpawner):
    def spawn(self, name: str, work: Callable[[], None]) -> None:
        self._guarded(name, work)()
