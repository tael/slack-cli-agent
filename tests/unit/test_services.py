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


class Test죽은실행기를드러낸다:
    """PeriodicRunner 의 순회는 Exception 만 잡는다. 그 밖의 BaseException 이
    나면 그 스레드만 끝나고 프로세스는 계속 산다. 그 주기 작업이 멈춘 것과
    아무 일도 없던 것이 로그에서 구분되지 않는다 (sca-2g0).
    """

    def test_아직_안_띄웠으면_전부_죽은_것으로_본다(self) -> None:
        group = ServiceGroup([make_runner("가")], name="시험")

        assert group.dead_runners() == ("가",)

    def test_띄운_뒤에는_죽은_것이_없다(self) -> None:
        group = ServiceGroup([make_runner("가"), make_runner("나")], name="시험")
        with group:
            assert group.dead_runners() == ()

    def test_스레드가_끝난_실행기를_이름으로_알려준다(self) -> None:
        죽는다 = PeriodicRunner(lambda: None, 3600.0, name="죽는다")
        산다 = make_runner("산다")
        group = ServiceGroup([죽는다, 산다], name="시험")
        with group:
            죽는다.stop()
            죽는다.join(timeout=2.0)

            assert group.dead_runners() == ("죽는다",)

    def test_감시_주기를_주면_죽은_것을_알린다(self) -> None:
        알림: list[str] = []
        죽는다 = PeriodicRunner(lambda: None, 3600.0, name="죽는다")
        group = ServiceGroup(
            [죽는다], name="시험", watch_interval_sec=3600.0, notify=알림.append
        )
        with group:
            죽는다.stop()
            죽는다.join(timeout=2.0)
            group.check_alive()

        assert len(알림) == 1
        assert "죽는다" in 알림[0]

    def test_같은_상태면_다시_알리지_않는다(self) -> None:
        알림: list[str] = []
        죽는다 = PeriodicRunner(lambda: None, 3600.0, name="죽는다")
        group = ServiceGroup(
            [죽는다], name="시험", watch_interval_sec=3600.0, notify=알림.append
        )
        with group:
            죽는다.stop()
            죽는다.join(timeout=2.0)
            group.check_alive()
            group.check_alive()

        assert len(알림) == 1

    def test_감시_주기를_주면_감시_실행기도_함께_뜬다(self) -> None:
        group = ServiceGroup([make_runner("가")], name="시험", watch_interval_sec=3600.0)
        with group:
            assert "시험_watch" in group.runner_names
            # 이름만 보면 안 띄운 것과 구분되지 않는다. 스레드를 본다.
            assert group._watch is not None and group._watch.is_running()

    def test_감시_주기가_없으면_감시_실행기를_안_만든다(self) -> None:
        group = ServiceGroup([make_runner("가")], name="시험")
        with group:
            assert group.runner_names == ("가",)
