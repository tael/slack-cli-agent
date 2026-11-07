"""Background jobs and event hooks a plugin can add (G1).

The original bot.py hung its welcome, shadow evaluation and DM report on
periodic loops and socket events, and ran every engine-calling path under
one `_slots` semaphore (bot.py:1151-1170). Without these hooks a plugin
could only add those by editing core code.

A job or hook that calls the engine takes a slot from the same pool the
worker's requests and the ingress reviews use (A16), so adding one cannot
push the bot past max_concurrent.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..core.periodic import PeriodicRunner
from ..core.slots import ConcurrencySlots
from ..core.spawn import TaskSpawner
from .base import BotPlugin

log = logging.getLogger(__name__)

#: The two long-running processes. Each job runs in exactly one, so two
#: processes never run the same job at once.
PROCESSES = ("worker", "ingress")


@dataclass(frozen=True)
class BackgroundJob:
    name: str
    run: Callable[[], object]
    interval_sec: float
    initial_delay_sec: float = 0.0
    # The worker by default: it is the process that already runs the
    # engine-calling loops (watch, learning batch).
    process: str = "worker"
    # Off for cheap bookkeeping, so it does not wait behind engine turns.
    uses_engine: bool = True

    def __post_init__(self) -> None:
        if self.process not in PROCESSES:
            raise ValueError(f"알 수 없는 프로세스 : {self.process} ({', '.join(PROCESSES)} 중 하나)")


@dataclass(frozen=True)
class EventHandler:
    """Runs on the ingress process, off the socket thread, after the core
    handlers for the same event type."""

    event_type: str
    name: str
    handle: Callable[[Mapping[str, Any]], None]
    uses_engine: bool = True


def capped(
    name: str, work: Callable[[], object], slots: ConcurrencySlots, wait_sec: float
) -> Callable[[], bool]:
    """Wraps `work` so it runs only while holding a slot.

    Waits up to `wait_sec` for one. A periodic job that gets none skips this
    round rather than queueing up: the next tick asks again.
    """

    def run() -> bool:
        token = slots.acquire(timeout=wait_sec)
        if token is None:
            log.warning("동시 실행 상한이 차서 이번 회차를 건너뛴다 : %s", name)
            return False
        try:
            work()
        finally:
            slots.release(token)
        return True

    return run


def _full_name(plugin: BotPlugin, name: str) -> str:
    # Two plugins may both call a job "daily"; the runner name is what the
    # service watch reports, so it has to say whose job it is.
    return f"{plugin.name}:{name}"


def background_runners(
    plugins: Sequence[BotPlugin],
    process: str,
    slots: ConcurrencySlots,
    wait_sec: float,
) -> list[PeriodicRunner]:
    runners: list[PeriodicRunner] = []
    for plugin in plugins:
        for job in plugin.background_jobs():
            if job.process != process:
                continue
            name = _full_name(plugin, job.name)
            task = capped(name, job.run, slots, wait_sec) if job.uses_engine else job.run
            runners.append(
                PeriodicRunner(
                    task, job.interval_sec, name=name, initial_delay_sec=job.initial_delay_sec
                )
            )
    return runners


def event_callbacks(
    plugins: Sequence[BotPlugin],
    spawn: TaskSpawner,
    slots: ConcurrencySlots,
    wait_sec: float,
) -> list[tuple[str, Callable[[Mapping[str, Any]], None]]]:
    """(event type, callback) pairs for SlackGateway.on.

    Each callback hands the work to `spawn` and returns: a hook that waits
    for a slot or calls the engine would otherwise hold the socket thread
    and every event behind it.
    """
    callbacks: list[tuple[str, Callable[[Mapping[str, Any]], None]]] = []
    for plugin in plugins:
        for hook in plugin.event_handlers():
            name = _full_name(plugin, hook.name)
            callbacks.append((hook.event_type, _spawning(name, hook, spawn, slots, wait_sec)))
    return callbacks


def _spawning(
    name: str,
    hook: EventHandler,
    spawn: TaskSpawner,
    slots: ConcurrencySlots,
    wait_sec: float,
) -> Callable[[Mapping[str, Any]], None]:
    def on_event(event: Mapping[str, Any]) -> None:
        def work() -> None:
            hook.handle(event)

        if not hook.uses_engine:
            spawn.spawn(name, work)
            return
        run = capped(name, work, slots, wait_sec)

        def run_capped() -> None:
            run()

        spawn.spawn(name, run_capped)

    return on_event
