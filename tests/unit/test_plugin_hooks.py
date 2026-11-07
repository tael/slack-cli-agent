"""플러그인 백그라운드 작업과 이벤트 훅 (G1).

마메치의 환영, 그림자 평가, DM 일일 보고는 주기 루프나 소켓 이벤트에
붙는다. BotPlugin 에 그 자리가 없으면 코어를 고쳐야만 붙일 수 있다.
엔진을 쓰는 작업은 요청, 점검과 같은 동시 실행 상한을 공유한다
(bot.py 의 _slots 하나를 6개 경로가 공유한 것과 같다).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from test_application import FakeSlackClient, write_profile

from slack_cli_agent.core.application import Application
from slack_cli_agent.core.slots import ThreadSlots
from slack_cli_agent.core.spawn import InlineTaskSpawner
from slack_cli_agent.plugin.base import BotPlugin
from slack_cli_agent.plugin.hooks import (
    BackgroundJob,
    EventHandler,
    background_runners,
    capped,
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


def test_자리가_있으면_실행하고_자리를_돌려준다() -> None:
    slots = ThreadSlots(1)
    calls: list[int] = []
    assert capped("j", lambda: calls.append(1), slots, wait_sec=0.1)() is True
    assert calls == [1]
    token = slots.acquire(timeout=0)
    assert token is not None


def test_상한이_차면_자리가_날_때까지_기다린다() -> None:
    slots = ThreadSlots(1)
    held = slots.acquire(timeout=0)
    assert held is not None
    ran = threading.Event()
    result: list[bool] = []
    worker = threading.Thread(
        target=lambda: result.append(capped("j", ran.set, slots, wait_sec=5)())
    )
    worker.start()
    time.sleep(0.2)
    assert not ran.is_set()
    slots.release(held)
    worker.join(2)
    assert ran.is_set()
    assert result == [True]


def test_기다려도_자리가_안_나면_이번_회차를_건너뛴다() -> None:
    slots = ThreadSlots(1)
    assert slots.acquire(timeout=0) is not None
    calls: list[int] = []
    assert capped("j", lambda: calls.append(1), slots, wait_sec=0.05)() is False
    assert calls == []


def test_엔진을_안_쓰는_작업은_자리를_잡지_않는다() -> None:
    slots = ThreadSlots(1)
    assert slots.acquire(timeout=0) is not None
    calls: list[int] = []
    job = BackgroundJob(
        name="tick", run=lambda: calls.append(1), interval_sec=60, uses_engine=False
    )
    (runner,) = background_runners([_Plugin([job])], "worker", slots, wait_sec=0.05)
    runner.start()
    try:
        deadline = time.time() + 2
        while not calls and time.time() < deadline:
            time.sleep(0.02)
    finally:
        runner.stop()
        runner.join(1)
    assert calls == [1]


def test_등록된_작업이_주기_실행된다() -> None:
    calls: list[float] = []
    job = BackgroundJob(name="tick", run=lambda: calls.append(time.time()), interval_sec=0.05)
    (runner,) = background_runners([_Plugin([job])], "worker", ThreadSlots(2), wait_sec=1)
    assert runner.name == "sample:tick"
    runner.start()
    try:
        deadline = time.time() + 2
        while len(calls) < 3 and time.time() < deadline:
            time.sleep(0.02)
    finally:
        runner.stop()
        runner.join(1)
    assert len(calls) >= 3


def test_프로세스별로_작업을_가른다() -> None:
    plugin = _Plugin(
        [
            BackgroundJob(name="w", run=lambda: None, interval_sec=1),
            BackgroundJob(name="i", run=lambda: None, interval_sec=1, process="ingress"),
        ]
    )
    slots = ThreadSlots(1)
    assert [r.name for r in background_runners([plugin], "worker", slots, 1)] == ["sample:w"]
    assert [r.name for r in background_runners([plugin], "ingress", slots, 1)] == ["sample:i"]


def test_알수없는_프로세스는_거부한다() -> None:
    with pytest.raises(ValueError):
        BackgroundJob(name="x", run=lambda: None, interval_sec=1, process="both")


def test_이벤트_훅이_게이트웨이에_붙고_자리를_잡아_실행된다() -> None:
    slots = ThreadSlots(1)
    seen: list[Mapping[str, Any]] = []
    held_during: list[bool] = []

    def on_join(event: Mapping[str, Any]) -> None:
        seen.append(event)
        held_during.append(slots.acquire(timeout=0) is None)

    plugin = _Plugin(handlers=[EventHandler(event_type="message", name="welcome", handle=on_join)])
    gateway = SlackGateway(client=None)
    for event_type, callback in event_callbacks([plugin], InlineTaskSpawner(), slots, 1):
        gateway.on(event_type, callback)
    gateway.dispatch("message", {"subtype": "channel_join", "user": "U1"})
    assert seen == [{"subtype": "channel_join", "user": "U1"}]
    assert held_during == [True]
    assert slots.acquire(timeout=0) is not None


def test_이벤트_훅_예외가_게이트웨이로_새지_않는다() -> None:
    def boom(event: Mapping[str, Any]) -> None:
        raise RuntimeError("x")

    plugin = _Plugin(handlers=[EventHandler(event_type="message", name="b", handle=boom)])
    gateway = SlackGateway(client=None)
    for event_type, callback in event_callbacks([plugin], InlineTaskSpawner(), ThreadSlots(1), 1):
        gateway.on(event_type, callback)
    gateway.dispatch("message", {})


def test_애플리케이션이_플러그인_작업을_프로세스별_서비스에_넣는다(tmp_path: Path) -> None:
    plugin = _Plugin(
        [
            BackgroundJob(name="w", run=lambda: None, interval_sec=60),
            BackgroundJob(name="i", run=lambda: None, interval_sec=60, process="ingress"),
        ],
        [EventHandler(event_type="member_joined_channel", name="h", handle=lambda e: None)],
    )
    app = Application(write_profile(tmp_path), FakeSlackClient(), plugins=[plugin])
    worker_names = app.worker_services(app.worker()).runner_names
    ingress_names = app.ingress_services(lambda reason: None).runner_names
    assert "sample:w" in worker_names and "sample:i" not in worker_names
    assert "sample:i" in ingress_names and "sample:w" not in ingress_names
    gateway = SlackGateway(client=None)
    app.ingress().register(gateway)
    assert gateway.handler_count("member_joined_channel") == 1
