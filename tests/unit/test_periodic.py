"""주기 실행기. 같은 작업을 일정 간격으로 되풀이한다.

원본은 `people_loop()` 처럼 함수 안에 `while` 무한 루프를 직접 썼다. 그러면
그 루프가 몇 회 돌았는지, 중단 요청에 언제 반응하는지를 시험할 수 없다 —
시험이 루프에 들어가면 나오지 못한다. 반복 구조를 클래스로 분리해 한 회차와
중단 조건을 각각 시험 대상으로 만든다.
"""

from __future__ import annotations

import threading

from slack_cli_agent.core.periodic import PeriodicRunner


class TestPeriodicRunner실행:
    def test_중단될_때까지_작업을_되풀이한다(self) -> None:
        calls: list[int] = []
        runner: PeriodicRunner

        def task() -> None:
            calls.append(len(calls))
            if len(calls) == 3:
                runner.stop()

        runner = PeriodicRunner(task, interval_sec=0)
        runner.run_until_stopped()
        assert calls == [0, 1, 2]

    def test_시작_전에_중단하면_한_번도_실행되지_않는다(self) -> None:
        """중단 요청이 먼저 온 상태로 시작하면 작업을 아예 실행하지 않는다.

        한 회차를 먼저 실행하고 나서 중단을 확인하면, 종료 중인 프로세스가
        슬랙 API 를 한 번 더 호출한다.
        """
        calls: list[int] = []
        runner = PeriodicRunner(lambda: calls.append(1), interval_sec=0)
        runner.stop()
        runner.run_until_stopped()
        assert calls == []

    def test_작업이_예외를_내도_반복이_멈추지_않는다(self) -> None:
        """한 회차의 실패가 이후 회차를 막지 않는다.

        슬랙 조회는 일시적으로 실패한다. 그 한 번에 반복이 끝나면 프로세스가
        살아 있는 동안 명부가 두 번 다시 갱신되지 않는다.
        """
        calls: list[int] = []
        runner: PeriodicRunner

        def task() -> None:
            calls.append(len(calls))
            if len(calls) == 3:
                runner.stop()
                return
            raise RuntimeError("일시 실패")

        runner = PeriodicRunner(task, interval_sec=0)
        runner.run_until_stopped()
        assert calls == [0, 1, 2]


class TestPeriodicRunner스레드:
    def test_start가_별도_스레드에서_돌고_stop으로_끝난다(self) -> None:
        done = threading.Event()
        calls: list[int] = []

        def task() -> None:
            calls.append(1)
            done.set()

        runner = PeriodicRunner(task, interval_sec=0)
        runner.start()
        try:
            assert done.wait(timeout=5), "주기 실행기가 5초 안에 작업을 한 번도 실행하지 않았다"
        finally:
            runner.stop()
            runner.join(timeout=5)
        assert calls

    def test_stop_후에는_스레드가_살아_있지_않다(self) -> None:
        runner = PeriodicRunner(lambda: None, interval_sec=0)
        runner.start()
        runner.stop()
        runner.join(timeout=5)
        assert not runner.is_running()

    def test_start를_두_번_불러도_스레드가_하나다(self) -> None:
        """중복 기동을 막는다. 스레드가 둘이면 같은 파일을 동시에 쓴다."""
        runner = PeriodicRunner(lambda: None, interval_sec=0)
        runner.start()
        first = runner.thread
        runner.start()
        try:
            assert runner.thread is first
        finally:
            runner.stop()
            runner.join(timeout=5)
