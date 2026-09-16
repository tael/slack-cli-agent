"""앱 상태를 스냅샷 출처 계약으로 잇는 어댑터.

원본 `state_snapshot()` 이 전역에서 직접 읽던 값을 실제 부품에서 가져온다.
"""

from __future__ import annotations

from slack_cli_agent.core.lifecycle import InflightCounter
from slack_cli_agent.observability.app_snapshot import ApplicationSnapshotSource


class FakeQueue:
    def __init__(self, pending: int = 0) -> None:
        self._pending = pending

    def counts(self) -> dict[str, int]:
        return {"queued": self._pending, "completed": 99}


class FakeSocketWatch:
    def error_timestamps(self) -> tuple[float, ...]:
        return (1.0, 2.0)

    def reconnect_timestamps(self) -> tuple[float, ...]:
        return (3.0,)


class FakeWatchJobs:
    def __init__(self, count: int | None = 4) -> None:
        self._count = count

    def open_count(self) -> int:
        if self._count is None:
            raise RuntimeError("조회 실패")
        return self._count


def make_source(**overrides):
    kwargs = {
        "inflight": InflightCounter(),
        "queue": FakeQueue(3),
        "socket_watch": FakeSocketWatch(),
        "watch_jobs": FakeWatchJobs(),
        "is_shutting_down": lambda: False,
        "started_at": 500.0,
    }
    kwargs.update(overrides)
    return ApplicationSnapshotSource(**kwargs)


class TestApplicationSnapshotSource:
    def test_진행중_건수를_카운터에서_읽는다(self) -> None:
        inflight = InflightCounter()
        inflight.enter()
        assert make_source(inflight=inflight).inflight_count() == 1

    def test_대기_건수를_큐_상태별_집계에서_읽는다(self) -> None:
        """완료된 것은 빼고 센다. 그것까지 세면 밀린 일이 있다고 읽힌다."""
        assert make_source().queued_threads() == {"queued": 3}

    def test_소켓_이력을_그대로_넘긴다(self) -> None:
        source = make_source()
        assert source.socket_error_timestamps() == (1.0, 2.0)
        assert source.socket_reconnect_timestamps() == (3.0,)

    def test_감시_작업_수를_읽는다(self) -> None:
        assert make_source().watch_job_count() == 4

    def test_감시_작업_조회가_실패하면_None_이다(self) -> None:
        """0 으로 적으면 등록된 것이 없는 것과 구분되지 않는다."""
        assert make_source(watch_jobs=FakeWatchJobs(None)).watch_job_count() is None

    def test_종료_여부와_기동_시각을_넘긴다(self) -> None:
        source = make_source(is_shutting_down=lambda: True)
        assert source.is_shutting_down() is True
        assert source.started_at() == 500.0

    def test_캐치업_대기는_아직_세지_않는다(self) -> None:
        """캐치업은 접수 시점에 바로 처리돼 대기 개념이 없다.

        원본에 있던 항목이라 계약에는 남기고 0 으로 둔다. 값을 지어내지 않는다.
        """
        assert make_source().catchup_pending_count() == 0


class Test소켓이_없는_프로세스:
    """스냅샷은 워커가 쓰는데 소켓은 접수기에만 있다(sca-qi5.3).

    워커의 로그 감시자는 소켓 로그를 한 줄도 못 본다. 실측 2026-09-17 — 접수기
    로그에는 세션 수립이 51회 있는데 워커가 쓴 state.json 의 재연결 누적은 0 이었다.
    """

    def test_감시자가_없으면_오류_이력이_None_이다(self) -> None:
        source = make_source(socket_watch=None)
        assert source.socket_error_timestamps() is None

    def test_재연결은_원장에서_읽는다(self) -> None:
        class Fake원장:
            def reconnect_timestamps(self) -> tuple[float, ...]:
                return (10.0, 20.0)

        source = make_source(socket_watch=None, connection_epochs=Fake원장())
        assert source.socket_reconnect_timestamps() == (10.0, 20.0)

    def test_원장_조회가_실패해도_스냅샷은_나간다(self) -> None:
        class Raising원장:
            def reconnect_timestamps(self) -> tuple[float, ...]:
                raise RuntimeError("조회 실패")

        source = make_source(socket_watch=None, connection_epochs=Raising원장())
        assert source.socket_reconnect_timestamps() == ()
