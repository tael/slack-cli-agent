"""ServiceGroup — 프로세스 하나가 띄우는 주기 실행기 묶음.

주기 실행기를 어디서 띄우고 끄는지가 `cli.py` 에 절차로 흩어져 있으면,
`Application` 에 새 실행기를 정의해도 CLI 에서 빠뜨리는 순간 그 동작은 어떤
실행 경로에서도 일어나지 않는다. 실제로 반복해서 그렇게 됐다 — 연결 점검,
끝난 작업 정리, 첨부 정리가 전부 같은 형태였다.

묶음으로 만들어 기동과 종료를 함께 하면, 새 실행기를 그 묶음에 넣는 것만으로
양쪽이 동시에 갖춰진다.
"""

from __future__ import annotations

import pytest

from slack_cli_agent.core.periodic import PeriodicRunner
from slack_cli_agent.core.services import ServiceGroup


def make_runner(name: str) -> PeriodicRunner:
    return PeriodicRunner(lambda: None, 3600.0, name=name)


class Test기동과종료:
    def test_들어간_실행기를_전부_띄운다(self) -> None:
        runners = [make_runner("가"), make_runner("나")]
        group = ServiceGroup(runners, name="시험")
        group.start()
        try:
            assert all(r.is_running() for r in runners)
        finally:
            group.stop()

    def test_종료하면_전부_멈춘다(self) -> None:
        runners = [make_runner("가"), make_runner("나")]
        group = ServiceGroup(runners, name="시험")
        group.start()
        group.stop()
        for r in runners:
            r.join(timeout=2.0)
        assert not any(r.is_running() for r in runners)

    def test_with_로_쓰면_빠져나갈_때_멈춘다(self) -> None:
        """종료를 `finally` 에 손으로 적으면 빠뜨린다. 그 자리를 없앤다."""
        runners = [make_runner("가"), make_runner("나")]
        group = ServiceGroup(runners, name="시험")
        with group:
            assert all(r.is_running() for r in runners)
        for r in runners:
            r.join(timeout=2.0)
        assert not any(r.is_running() for r in runners)

    def test_본문에서_예외가_나도_멈춘다(self) -> None:
        """예외로 빠져나갈 때 안 멈추면 그 스레드가 프로세스 종료까지 슬랙을 계속 부른다."""
        runners = [make_runner("가")]
        group = ServiceGroup(runners, name="시험")
        with pytest.raises(RuntimeError, match="본문 실패"), group:
            raise RuntimeError("본문 실패")
        runners[0].join(timeout=2.0)
        assert not runners[0].is_running()

    def test_하나가_기동에_실패해도_나머지를_멈춘다(self) -> None:
        """앞의 것만 뜬 채로 남으면 그 스레드가 회수되지 않는다."""

        class 기동실패(PeriodicRunner):
            def start(self) -> None:
                raise RuntimeError("기동 실패")

        정상 = make_runner("정상")
        실패 = 기동실패(lambda: None, 3600.0, name="실패")
        group = ServiceGroup([정상, 실패], name="시험")
        with pytest.raises(RuntimeError, match="기동 실패"):
            group.start()
        정상.join(timeout=2.0)
        assert not 정상.is_running()


class Test묶음내용:
    def test_들어간_실행기의_이름을_알려준다(self) -> None:
        """무엇이 그 프로세스에서 도는지를 시험이 대조할 수 있어야 한다."""
        group = ServiceGroup([make_runner("가"), make_runner("나")], name="시험")
        assert group.runner_names == ("가", "나")

    def test_빈_묶음도_만들_수_있다(self) -> None:
        group = ServiceGroup([], name="빈것")
        with group:
            pass
        assert group.runner_names == ()
