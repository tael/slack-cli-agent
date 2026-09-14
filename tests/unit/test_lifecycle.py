"""core.lifecycle 단위 시험.

원본 bot.py 의 enter_work/leave_work/inflight_count/on_terminate 를
이관한 InflightCounter/GracefulShutdown 의 계약을 확인한다. 실제
signal.signal 과 time.sleep 을 쓰지 않는다 — 둘 다 주입해 가짜로 바꾼다.
"""

from __future__ import annotations

import pytest

from slack_cli_agent.core.lifecycle import GracefulShutdown, InflightCounter


class TestInflightCounter:
    def test_시작값은_0이다(self):
        counter = InflightCounter()
        assert counter.count == 0

    def test_enter는_증가시키고_leave는_감소시킨다(self):
        counter = InflightCounter()
        counter.enter()
        counter.enter()
        assert counter.count == 2
        counter.leave()
        assert counter.count == 1

    def test_work_컨텍스트는_진입시_증가_종료시_감소한다(self):
        counter = InflightCounter()
        with counter.work():
            assert counter.count == 1
        assert counter.count == 0

    def test_work_컨텍스트는_예외가_나도_감소한다(self):
        counter = InflightCounter()
        with pytest.raises(ValueError):
            with counter.work():
                assert counter.count == 1
                raise ValueError("작업 중 예외")
        assert counter.count == 0


class TestGracefulShutdown:
    def test_대기중_요청이_없으면_바로_완료_콜백을_부른다(self):
        counter = InflightCounter()
        completed = []
        shutdown = GracefulShutdown(
            counter,
            grace_sec=5,
            sleep_fn=lambda sec: None,
            on_shutdown_complete=lambda: completed.append(True),
        )
        shutdown.trigger()
        assert completed == [True]
        assert shutdown.is_shutting_down is True

    def test_진행중_요청이_대기중에_끝나면_기다린_뒤_종료한다(self):
        counter = InflightCounter()
        counter.enter()
        counter.enter()
        sleep_calls = []

        def fake_sleep(sec):
            sleep_calls.append(sec)
            # 두 번째, 네 번째 대기에서 각각 하나씩 끝난다고 가정한다.
            if len(sleep_calls) == 2:
                counter.leave()
            if len(sleep_calls) == 4:
                counter.leave()

        completed = []
        shutdown = GracefulShutdown(
            counter,
            grace_sec=10,
            sleep_fn=fake_sleep,
            poll_interval_sec=0.5,
            on_shutdown_complete=lambda: completed.append(True),
        )
        shutdown.trigger()
        assert counter.count == 0
        assert completed == [True]
        assert len(sleep_calls) == 4

    def test_대기상한을_넘기면_타임아웃_콜백에_남은_건수를_준다(self):
        counter = InflightCounter()
        counter.enter()
        timed_out = []
        shutdown = GracefulShutdown(
            counter,
            grace_sec=1.0,
            sleep_fn=lambda sec: None,
            poll_interval_sec=0.5,
            on_shutdown_timeout=lambda left: timed_out.append(left),
        )
        shutdown.trigger()
        assert timed_out == [1]
        assert counter.count == 1

    def test_실제_time_sleep을_쓰지_않는다(self, monkeypatch):
        import time as time_module

        def boom(sec):
            raise AssertionError("실제 time.sleep 이 호출됐다")

        monkeypatch.setattr(time_module, "sleep", boom)
        counter = InflightCounter()
        counter.enter()
        shutdown = GracefulShutdown(
            counter,
            grace_sec=1.0,
            sleep_fn=lambda sec: counter.leave(),
            poll_interval_sec=0.5,
        )
        shutdown.trigger()
        assert counter.count == 0

    def test_두번째_trigger는_무시한다(self):
        counter = InflightCounter()
        starts = []
        shutdown = GracefulShutdown(
            counter,
            grace_sec=5,
            sleep_fn=lambda sec: None,
            on_shutdown_start=lambda inflight: starts.append(inflight),
        )
        shutdown.trigger()
        shutdown.trigger()
        assert starts == [0]

    def test_trigger_시작시_현재_진행중_건수를_시작_콜백에_준다(self):
        counter = InflightCounter()
        counter.enter()
        starts = []
        shutdown = GracefulShutdown(
            counter,
            grace_sec=5,
            sleep_fn=lambda sec: counter.leave(),
            on_shutdown_start=lambda inflight: starts.append(inflight),
        )
        shutdown.trigger()
        assert starts == [1]

    def test_register는_주입된_등록함수로_신호를_건다(self):
        registered = {}

        def fake_register(sig, handler):
            registered[sig] = handler

        shutdown = GracefulShutdown(
            InflightCounter(),
            grace_sec=5,
            sleep_fn=lambda sec: None,
            signal_register=fake_register,
        )
        shutdown.register(signals=(15, 2))
        assert set(registered.keys()) == {15, 2}

        # 등록된 핸들러를 실제로 불러도 종료 절차가 시작돼야 한다.
        registered[15](15, None)
        assert shutdown.is_shutting_down is True

    def test_register는_실제_signal_signal을_기본으로_쓰지_않도록_주입해야한다(self):
        # signal_register 를 안 주면 signal.signal 이 기본값이 되므로,
        # 이 시험은 반드시 주입해서 실제 프로세스 신호를 안 건드린다.
        calls = []
        shutdown = GracefulShutdown(
            InflightCounter(),
            grace_sec=5,
            sleep_fn=lambda sec: None,
            signal_register=lambda sig, handler: calls.append(sig),
        )
        shutdown.register(signals=(15,))
        assert calls == [15]

    def test_exit_fn이_주어지면_종료_후_호출된다(self):
        exits = []
        shutdown = GracefulShutdown(
            InflightCounter(),
            grace_sec=5,
            sleep_fn=lambda sec: None,
            exit_fn=lambda: exits.append(True),
        )
        shutdown.trigger()
        assert exits == [True]

    def test_exit_fn이_없어도_예외없이_끝난다(self):
        shutdown = GracefulShutdown(
            InflightCounter(),
            grace_sec=5,
            sleep_fn=lambda sec: None,
        )
        shutdown.trigger()  # 예외가 나지 않아야 한다
        assert shutdown.is_shutting_down is True
