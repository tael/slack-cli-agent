"""플러그인 백그라운드 작업과 이벤트 훅 (G1).

마메치의 환영, 그림자 평가, DM 일일 보고는 주기 루프나 소켓 이벤트에
붙는다. BotPlugin 에 그 자리가 없으면 코어를 고쳐야만 붙일 수 있다.
엔진을 쓰는 작업은 요청, 점검과 같은 동시 실행 상한을 공유한다
(bot.py 의 _slots 하나를 6개 경로가 공유한 것과 같다).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from test_application import FakeSlackClient, write_profile

from slack_cli_agent.auth.principal import Principal, TrustLevel
from slack_cli_agent.core.application import Application
from slack_cli_agent.core.slots import ThreadSlots
from slack_cli_agent.core.spawn import InlineTaskSpawner, TaskSpawner
from slack_cli_agent.plugin.base import BotPlugin
from slack_cli_agent.plugin.hooks import (
    BackgroundJob,
    EngineSlot,
    EventHandler,
    HookEvent,
    SlotUnavailable,
    background_runners,
    engine_slot,
    event_callbacks,
)
from slack_cli_agent.slack.gateway import SlackGateway


class _Plugin(BotPlugin):
    name = "sample"

    def __init__(
        self,
        jobs: Sequence[BackgroundJob] = (),
        handlers: Sequence[EventHandler] = (),
    ) -> None:
        self._jobs = tuple(jobs)
        self._handlers = tuple(handlers)

    def background_jobs(self) -> Sequence[BackgroundJob]:
        return self._jobs

    def event_handlers(self) -> Sequence[EventHandler]:
        return self._handlers


def test_기본_플러그인은_작업과_훅이_없다() -> None:
    class Bare(BotPlugin):
        name = "bare"

    assert tuple(Bare().background_jobs()) == ()
    assert tuple(Bare().event_handlers()) == ()


class _Policy:
    """Owner U0 passes anywhere; others only in registered channel C1."""

    def principal_for(self, channel: str, user: str) -> Principal:
        trust = TrustLevel.OWNER if user == "U0" else TrustLevel.GENERAL
        return Principal(user, channel, trust, channel.startswith("D"))

    def accepts_request(self, principal: Principal) -> bool:
        return principal.trust is TrustLevel.OWNER or principal.channel == "C1"


class _CountingSpawner(TaskSpawner):
    def __init__(self) -> None:
        self.names: list[str] = []

    def spawn(self, name: str, work: Any) -> None:
        self.names.append(name)
        threading.Thread(target=self._guarded(name, work), daemon=True).start()


def _wire(
    hooks: Sequence[EventHandler],
    slots: ThreadSlots,
    spawn: TaskSpawner | None = None,
    wait_sec: float = 1,
    max_pending: int = 16,
) -> SlackGateway:
    gateway = SlackGateway(client=None)
    for event_type, callback in event_callbacks(
        [_Plugin(handlers=hooks)],
        spawn or InlineTaskSpawner(),
        slots,
        wait_sec,
        policy=_Policy(),
        self_user_id=lambda: "UBOT",
        max_pending=max_pending,
    ):
        gateway.on(event_type, callback)
    return gateway


def _wait_until(cond: Any, sec: float = 2) -> bool:
    deadline = time.time() + sec
    while not cond() and time.time() < deadline:
        time.sleep(0.02)
    return bool(cond())


def test_엔진_구간에서만_자리를_잡고_돌려준다() -> None:
    slots = ThreadSlots(1)
    engine = engine_slot(slots, wait_sec=0.1)
    with engine():
        assert slots.acquire(timeout=0) is None
    token = slots.acquire(timeout=0)
    assert token is not None


def test_상한이_차면_자리가_날_때까지_기다린다() -> None:
    slots = ThreadSlots(1)
    held = slots.acquire(timeout=0)
    assert held is not None
    ran = threading.Event()

    def enter() -> None:
        with engine_slot(slots, wait_sec=5, poll_sec=0.05)():
            ran.set()

    worker = threading.Thread(target=enter)
    worker.start()
    time.sleep(0.2)
    assert not ran.is_set()
    slots.release(held)
    worker.join(2)
    assert ran.is_set()


def test_기다려도_자리가_안_나면_SlotUnavailable() -> None:
    slots = ThreadSlots(1)
    assert slots.acquire(timeout=0) is not None
    with pytest.raises(SlotUnavailable), engine_slot(slots, wait_sec=0.05)():
        pytest.fail("자리 없이 실행됐다")


def test_판정만_하는_작업은_자리를_잡지_않는다() -> None:
    slots = ThreadSlots(1)
    assert slots.acquire(timeout=0) is not None
    calls: list[int] = []
    job = BackgroundJob(name="tick", run=lambda engine: calls.append(1), interval_sec=60)
    (runner,) = background_runners([_Plugin([job])], "worker", slots, wait_sec=0.05)
    runner.start()
    try:
        assert _wait_until(lambda: calls)
    finally:
        runner.stop()
        runner.join(1)


def test_자리가_없으면_이번_회차를_건너뛰고_다음_회차에_다시_묻는다() -> None:
    slots = ThreadSlots(1)
    held = slots.acquire(timeout=0)
    ticks: list[int] = []
    ran: list[int] = []

    def run(engine: EngineSlot) -> None:
        ticks.append(1)
        with engine():
            ran.append(1)

    job = BackgroundJob(name="t", run=run, interval_sec=0.05)
    (runner,) = background_runners([_Plugin([job])], "worker", slots, wait_sec=0.05)
    runner.start()
    try:
        assert _wait_until(lambda: len(ticks) >= 2)
        assert ran == []
        slots.release(held)
        assert _wait_until(lambda: ran)
    finally:
        runner.stop()
        runner.join(1)


def test_자리_대기_중_stop_하면_바로_멈춘다() -> None:
    slots = ThreadSlots(1)
    assert slots.acquire(timeout=0) is not None
    entered = threading.Event()
    ran: list[int] = []

    def run(engine: EngineSlot) -> None:
        entered.set()
        with engine():
            ran.append(1)

    job = BackgroundJob(name="t", run=run, interval_sec=60)
    (runner,) = background_runners([_Plugin([job])], "worker", slots, wait_sec=900)
    runner.start()
    assert entered.wait(2)
    started = time.time()
    runner.stop()
    runner.join(3)
    assert not runner.is_running()
    assert time.time() - started < 2.5
    assert ran == []


def test_등록된_작업이_주기_실행된다() -> None:
    calls: list[float] = []
    job = BackgroundJob(name="tick", run=lambda engine: calls.append(time.time()), interval_sec=0.05)
    (runner,) = background_runners([_Plugin([job])], "worker", ThreadSlots(2), wait_sec=1)
    assert runner.name == "sample:tick"
    runner.start()
    try:
        assert _wait_until(lambda: len(calls) >= 3)
    finally:
        runner.stop()
        runner.join(1)


def test_프로세스별로_작업을_가른다() -> None:
    plugin = _Plugin(
        [
            BackgroundJob(name="w", run=lambda engine: None, interval_sec=1),
            BackgroundJob(name="i", run=lambda engine: None, interval_sec=1, process="ingress"),
        ]
    )
    slots = ThreadSlots(1)
    assert [r.name for r in background_runners([plugin], "worker", slots, 1)] == ["sample:w"]
    assert [r.name for r in background_runners([plugin], "ingress", slots, 1)] == ["sample:i"]


def test_알수없는_프로세스는_거부한다() -> None:
    with pytest.raises(ValueError):
        BackgroundJob(name="x", run=lambda engine: None, interval_sec=1, process="both")


def test_이벤트_훅이_엔진_구간에서_자리를_잡고_요청자_등급을_받는다() -> None:
    slots = ThreadSlots(1)
    seen: list[HookEvent] = []
    held_during: list[bool] = []

    def on_msg(call: HookEvent) -> None:
        seen.append(call)
        with call.engine():
            held_during.append(slots.acquire(timeout=0) is None)

    gateway = _wire([EventHandler(event_type="message", name="h", handle=on_msg)], slots)
    gateway.dispatch("message", {"channel": "C1", "user": "U1", "ts": "1.0"})
    assert [c.principal.trust for c in seen] == [TrustLevel.GENERAL]
    assert held_during == [True]
    assert slots.acquire(timeout=0) is not None


def test_거른_이벤트는_스레드도_자리도_쓰지_않는다() -> None:
    slots = ThreadSlots(1)
    spawn = _CountingSpawner()
    seen: list[HookEvent] = []
    hook = EventHandler(
        event_type="message",
        name="join",
        handle=seen.append,
        accepts=lambda e: e.get("subtype") == "channel_join",
    )
    gateway = _wire([hook], slots, spawn)
    for i in range(50):
        gateway.dispatch("message", {"channel": "C1", "user": "U1", "ts": f"{i}.0"})
    assert spawn.names == []
    assert slots.acquire(timeout=0) is not None


def test_봇_재전송_미허용_이벤트는_훅에_가지_않는다() -> None:
    seen: list[HookEvent] = []
    gateway = _wire([EventHandler(event_type="message", name="h", handle=seen.append)], ThreadSlots(1))
    gateway.dispatch("message", {"channel": "C1", "user": "UBOT", "ts": "1.0"})
    gateway.dispatch("message", {"channel": "C1", "user": "U2", "bot_id": "B", "ts": "2.0"})
    gateway.dispatch("message", {"channel": "C9", "user": "U1", "ts": "3.0"})
    gateway.dispatch("message", {"channel": "D1", "user": "U1", "ts": "4.0"})
    gateway.dispatch("message", {"channel": "C1", "user": "U1", "ts": "5.0"})
    gateway.dispatch("message", {"channel": "C1", "user": "U1", "ts": "5.0"})
    assert [c.event["ts"] for c in seen] == ["5.0"]


def test_audience_all_은_미등록_채널도_GENERAL_로_넘긴다() -> None:
    seen: list[HookEvent] = []
    hook = EventHandler(event_type="message", name="h", handle=seen.append, audience="all")
    gateway = _wire([hook], ThreadSlots(1))
    gateway.dispatch("message", {"channel": "C9", "user": "U1", "ts": "1.0"})
    assert [(c.principal.channel, c.principal.trust) for c in seen] == [("C9", TrustLevel.GENERAL)]


def test_자리를_못_얻은_이벤트는_실패_통지된다() -> None:
    slots = ThreadSlots(1)
    assert slots.acquire(timeout=0) is not None
    dropped: list[HookEvent] = []

    def handle(call: HookEvent) -> None:
        with call.engine():
            pytest.fail("자리 없이 실행됐다")

    hook = EventHandler(event_type="message", name="h", handle=handle, on_unavailable=dropped.append)
    gateway = _wire([hook], slots, wait_sec=0.05)
    gateway.dispatch("message", {"channel": "C1", "user": "U1", "ts": "1.0"})
    assert [c.event["ts"] for c in dropped] == ["1.0"]


def test_자리가_찬_채_이벤트가_몰려도_대기는_상한까지만_쌓인다() -> None:
    slots = ThreadSlots(1)
    held = slots.acquire(timeout=0)
    spawn = _CountingSpawner()
    dropped: list[HookEvent] = []
    ran: list[str] = []

    def handle(call: HookEvent) -> None:
        with call.engine():
            ran.append(call.event["ts"])

    hook = EventHandler(event_type="message", name="h", handle=handle, on_unavailable=dropped.append)
    gateway = _wire([hook], slots, spawn, wait_sec=5, max_pending=3)
    for i in range(20):
        gateway.dispatch("message", {"channel": "C1", "user": "U1", "ts": f"{i}.0"})
    assert len(spawn.names) == 3
    assert len(dropped) == 17
    slots.release(held)
    assert _wait_until(lambda: len(ran) == 3)


def test_이벤트_훅_예외가_게이트웨이로_새지_않는다() -> None:
    def boom(call: HookEvent) -> None:
        raise RuntimeError("x")

    gateway = _wire([EventHandler(event_type="message", name="b", handle=boom)], ThreadSlots(1))
    gateway.dispatch("message", {"channel": "C1", "user": "U1", "ts": "1.0"})


def test_message_훅을_붙여도_코어_핸들러가_먼저_돈다(tmp_path: Path) -> None:
    hook = EventHandler(event_type="message", name="h", handle=lambda call: None)
    app = Application(write_profile(tmp_path), FakeSlackClient(), plugins=[_Plugin(handlers=[hook])])
    ingress = app.ingress()
    gateway = SlackGateway(client=None)
    ingress.register(gateway)
    handlers = gateway._handlers["message"]
    assert len(handlers) == 2
    assert handlers[0] == ingress.handle_message


def test_애플리케이션이_플러그인_작업을_프로세스별_서비스에_넣는다(tmp_path: Path) -> None:
    plugin = _Plugin(
        [
            BackgroundJob(name="w", run=lambda engine: None, interval_sec=60),
            BackgroundJob(name="i", run=lambda engine: None, interval_sec=60, process="ingress"),
        ],
        [EventHandler(event_type="member_joined_channel", name="h", handle=lambda call: None)],
    )
    app = Application(write_profile(tmp_path), FakeSlackClient(), plugins=[plugin])
    worker_names = app.worker_services(app.worker()).runner_names
    ingress_names = app.ingress_services(lambda reason: None).runner_names
    assert "sample:w" in worker_names and "sample:i" not in worker_names
    assert "sample:i" in ingress_names and "sample:w" not in ingress_names
    gateway = SlackGateway(client=None)
    app.ingress().register(gateway)
    assert gateway.handler_count("member_joined_channel") == 1
