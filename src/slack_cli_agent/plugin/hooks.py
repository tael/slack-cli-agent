"""Background jobs and event hooks a plugin can add (G1).

The original bot.py hung its welcome, shadow evaluation and DM report on
periodic loops and socket events, and ran every engine-calling path under
one `_slots` semaphore (bot.py:1151-1170). Without these hooks a plugin
could only add those by editing core code.

A slot from the pool the worker's requests and the ingress reviews use (A16)
is taken only inside `with engine():`, the section that calls the engine. A
tick that only checks whether something is due, or a hook that decides to
ignore an event, takes none: the core reviews follow the same rule (a refused
reaction takes no slot).

Event hooks see only live socket events. Catch-up after a restart, queue
retries and the fallback-engine turn go through the worker, not the socket
handlers, so an event that arrived while the ingress was down never reaches a
hook. A plugin that must not miss one (a welcome, say) has to sweep for it in
a background job.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from ..auth.principal import Principal
from ..core.periodic import PeriodicRunner
from ..core.slots import ConcurrencySlots
from ..core.spawn import TaskSpawner
from ..reliability.dedup import DeduplicationTracker
from .base import BotPlugin

log = logging.getLogger(__name__)

#: The two long-running processes. Each job runs in exactly one, so two
#: processes never run the same job at once.
PROCESSES = ("worker", "ingress")

#: What a job or hook calls around its engine call: `with engine(): ...`.
#: Raises SlotUnavailable when no slot came free in time.
EngineSlot = Callable[[], AbstractContextManager[None]]


class SlotUnavailable(Exception):
    """No slot came free within the wait, or the runner is stopping."""


@dataclass(frozen=True)
class BackgroundJob:
    name: str
    # Gets the slot section; a run that never enters it takes no slot.
    run: Callable[[EngineSlot], object]
    interval_sec: float
    initial_delay_sec: float = 0.0
    # The worker by default: it is the process that already runs the
    # engine-calling loops (watch, learning batch).
    process: str = "worker"

    def __post_init__(self) -> None:
        if self.process not in PROCESSES:
            raise ValueError(f"알 수 없는 프로세스 : {self.process} ({', '.join(PROCESSES)} 중 하나)")


@dataclass(frozen=True)
class HookEvent:
    event: Mapping[str, Any]
    # The requester's tier, so a hook can keep writes to the owner the way
    # the core does (non-owners get read-only turns).
    principal: Principal
    engine: EngineSlot


@dataclass(frozen=True)
class EventHandler:
    """Runs on the ingress process, after the core handlers for the same
    event type. Gated like a core request before any thread or slot: events
    from bots, redeliveries, events `accepts` turns down and, with audience
    "allowed", events the access policy refuses are dropped on the socket
    thread."""

    event_type: str
    name: str
    handle: Callable[[HookEvent], None]
    # Cheap, no I/O: it runs on the socket thread.
    accepts: Callable[[Mapping[str, Any]], bool] = lambda event: True
    # "all" also passes unregistered channels and strangers' DMs (a welcome
    # for someone joining); the hook then gets their GENERAL principal.
    audience: Literal["allowed", "all"] = "allowed"
    # Called when an accepted event could not run (no slot in time, too many
    # pending). There is no later round for an event, so this is the place
    # to tell the user or record it for a retry.
    on_unavailable: Callable[[HookEvent], None] | None = None


class _Policy(Protocol):
    def principal_for(self, channel: str, user: str) -> Principal: ...
    def accepts_request(self, principal: Principal) -> bool: ...


def engine_slot(
    slots: ConcurrencySlots,
    wait_sec: float,
    stopping: Callable[[], bool] = lambda: False,
    poll_sec: float = 1.0,
) -> EngineSlot:
    """Waits up to `wait_sec` in `poll_sec` steps, giving up early once
    `stopping` says so, so a stop() does not wait out a whole request."""

    @contextmanager
    def section() -> Iterator[None]:
        remaining = wait_sec
        token = None
        while token is None:
            if stopping():
                raise SlotUnavailable("stopping")
            step = min(poll_sec, max(0.0, remaining))
            token = slots.acquire(timeout=step)
            remaining -= step
            if token is None and remaining <= 0:
                raise SlotUnavailable(f"no slot in {wait_sec}s")
        try:
            yield
        finally:
            slots.release(token)

    return section


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
            runners.append(_job_runner(_full_name(plugin, job.name), job, slots, wait_sec))
    return runners


def _job_runner(name: str, job: BackgroundJob, slots: ConcurrencySlots, wait_sec: float) -> PeriodicRunner:
    holder: list[PeriodicRunner] = []
    engine = engine_slot(slots, wait_sec, stopping=lambda: holder[0].is_stopping())

    def tick() -> None:
        try:
            job.run(engine)
        except SlotUnavailable as exc:
            # The next tick asks again, so skipping this round loses nothing.
            log.warning("동시 실행 자리를 얻지 못해 이번 회차를 건너뛴다 : %s : %s", name, exc)

    holder.append(PeriodicRunner(tick, job.interval_sec, name=name, initial_delay_sec=job.initial_delay_sec))
    return holder[0]


def event_callbacks(
    plugins: Sequence[BotPlugin],
    spawn: TaskSpawner,
    slots: ConcurrencySlots,
    wait_sec: float,
    *,
    policy: _Policy,
    self_user_id: Callable[[], str] = lambda: "",
    max_pending: int = 16,
) -> list[tuple[str, Callable[[Mapping[str, Any]], None]]]:
    """(event type, callback) pairs for SlackGateway.on.

    An event that passes the gate goes to `spawn`, so a hook waiting for a
    slot does not hold the socket thread. At most `max_pending` such events
    run or wait at once per hook; past that `on_unavailable` is told, so a
    burst cannot grow threads without bound.
    """
    engine = engine_slot(slots, wait_sec)
    callbacks: list[tuple[str, Callable[[Mapping[str, Any]], None]]] = []
    for plugin in plugins:
        for hook in plugin.event_handlers():
            name = _full_name(plugin, hook.name)
            gate = _Gate(hook, policy, self_user_id)
            callbacks.append((hook.event_type, _spawning(name, hook, gate, spawn, engine, max_pending)))
    return callbacks


class _Gate:
    """The core request checks, ahead of any thread: own and bot events,
    redeliveries (a dedup of its own, as the core already recorded the same
    ts), `accepts`, then the access policy."""

    def __init__(self, hook: EventHandler, policy: _Policy, self_user_id: Callable[[], str]):
        self._hook = hook
        self._policy = policy
        self._self_user_id = self_user_id
        self._dedup = DeduplicationTracker()

    def principal(self, event: Mapping[str, Any]) -> Principal | None:
        user = str(event.get("user") or "")
        if event.get("bot_id") or (user and user == self._self_user_id()):
            return None
        if not self._hook.accepts(event):
            return None
        principal = self._policy.principal_for(str(event.get("channel") or ""), user)
        if self._hook.audience == "allowed" and not self._policy.accepts_request(principal):
            return None
        key = str(event.get("ts") or event.get("event_ts") or "")
        if key and self._dedup.already_seen_event(principal.channel, key):
            return None
        return principal


def _spawning(
    name: str,
    hook: EventHandler,
    gate: _Gate,
    spawn: TaskSpawner,
    engine: EngineSlot,
    max_pending: int,
) -> Callable[[Mapping[str, Any]], None]:
    pending = threading.BoundedSemaphore(max(1, max_pending))

    def unavailable(call: HookEvent, reason: str) -> None:
        log.error("훅 이벤트를 실행하지 못했다 : %s : %s : %s", name, reason, call.event.get("ts"))
        if hook.on_unavailable is None:
            return
        try:
            hook.on_unavailable(call)
        except Exception:
            log.exception("훅 실패 통지 실패 : %s", name)

    def on_event(event: Mapping[str, Any]) -> None:
        principal = gate.principal(event)
        if principal is None:
            return
        call = HookEvent(event=event, principal=principal, engine=engine)
        if not pending.acquire(blocking=False):
            unavailable(call, f"대기 {max_pending}건 초과")
            return

        def work() -> None:
            try:
                hook.handle(call)
            except SlotUnavailable as exc:
                unavailable(call, str(exc))
            finally:
                pending.release()

        spawn.spawn(name, work)

    return on_event
