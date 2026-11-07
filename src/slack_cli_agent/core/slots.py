"""One cap on engine calls, shared by the worker and the ingress reviews.

The original bot.py ran requests and emoji reviews (postmortem, debug, format
check) under one `_slots` semaphore (bot.py:1166), so a burst of reactions
waited behind MAX_CONCURRENT like any request. Here the reviews run on
ingress threads and requests on the worker, two processes, so an in-process
semaphore cannot hold both: reviews ran with no cap at all (A16).

FileSlots holds the cap across processes with one flock per slot file. The
kernel drops a lock when its holder dies, so a crashed process cannot leak a
slot the way a counter row in the database would.
"""

from __future__ import annotations

import fcntl
import os
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path

#: What acquire hands back; release takes it again. None means no slot.
SlotToken = object


class ConcurrencySlots(ABC):
    @abstractmethod
    def acquire(self, timeout: float | None = None) -> SlotToken | None:
        """Waits up to `timeout` seconds (None waits without end) and returns
        a token, or None when no slot came free in time."""

    @abstractmethod
    def release(self, token: SlotToken) -> None: ...


class ThreadSlots(ConcurrencySlots):
    """In-process cap, for a single process or a test."""

    def __init__(self, size: int) -> None:
        self._free = threading.Semaphore(max(1, size))

    def acquire(self, timeout: float | None = None) -> SlotToken | None:
        return self if self._free.acquire(timeout=timeout) else None

    def release(self, token: SlotToken) -> None:
        self._free.release()


class FileSlots(ConcurrencySlots):
    """Cross-process cap: `size` lock files in `directory`, one flock each.

    flock belongs to the open file description, so two threads of the same
    process that each open the file conflict just as two processes do.
    """

    #: How often a waiter looks for a free slot again. Short next to an
    #: engine turn, long enough that a full pool does not spin a core.
    poll_sec = 0.2

    def __init__(
        self,
        directory: Path,
        size: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._directory = directory
        self._size = max(1, size)
        self._clock = clock
        self._sleep = sleep

    def acquire(self, timeout: float | None = None) -> SlotToken | None:
        deadline = None if timeout is None else self._clock() + timeout
        while True:
            fd = self._try_any()
            if fd is not None:
                return fd
            if deadline is not None and self._clock() >= deadline:
                return None
            wait = self.poll_sec if deadline is None else min(
                self.poll_sec, max(0.0, deadline - self._clock())
            )
            self._sleep(wait)

    def release(self, token: SlotToken) -> None:
        assert isinstance(token, int)
        try:
            fcntl.flock(token, fcntl.LOCK_UN)
        finally:
            os.close(token)

    def _try_any(self) -> int | None:
        self._directory.mkdir(parents=True, exist_ok=True)
        for index in range(self._size):
            fd = os.open(self._directory / f"slot-{index}.lock", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                continue
            return fd
        return None
