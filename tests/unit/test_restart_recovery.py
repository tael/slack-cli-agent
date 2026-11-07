"""재기동 위임, PID교체 판정, 롤백 분기, 네트워크 장애 감시, 미완료 보고.

~/Projects/mametchi-slack-bot 의 apply.sh/apply-restart.sh/restart.sh/
watchdog.py 를 코드로만 확인해 기대값을 정했다 (실행 없이 순수 로직만 뽑음).
turn 의 Seatbelt 샌드박스는 launchctl kickstart 를 거부하므로, 봇 턴은
재기동을 직접 하지 않고 요청 표식만 남긴다. 실제 재기동/프로세스 종료/
네트워크 호출은 하지 않는다 — 전부 주입한 가짜로 돈다.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from slack_cli_agent.reliability.outage import NetworkOutageEscalation, RealertGate
from slack_cli_agent.reliability.pending_report import (
    PendingWork,
    PendingWorkMarker,
    assemble_recovery_report,
)
from slack_cli_agent.reliability.restart import (
    ApplyRequestWriter,
    RestartLock,
    restart_confirmed,
    should_rollback,
)


class Clock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class Test재기동요청표식:
    """봇 턴은 launchctl 을 직접 못 부른다. 표식만 남기고 감시기에 넘긴다."""

    def test_타임스탬프를_첫줄에_남긴다(self, tmp_path: Path) -> None:
        path = tmp_path / "apply-request"
        writer = ApplyRequestWriter(path=path, now=lambda: 1234.0)

        writer.write()

        첫줄 = path.read_text(encoding="utf-8").splitlines()[0]
        assert 첫줄 == "1234"

    def test_턴_식별자를_둘째줄에_남기면_그_요청만_재기동을_기다리지_않는다(self, tmp_path: Path) -> None:
        path = tmp_path / "apply-request"
        writer = ApplyRequestWriter(path=path, now=lambda: 1234.0)

        writer.write(turn_id="T0912")

        줄들 = path.read_text(encoding="utf-8").splitlines()
        assert 줄들 == ["1234", "T0912"]

    def test_턴_식별자가_없으면_한_줄만_남는다(self, tmp_path: Path) -> None:
        path = tmp_path / "apply-request"
        writer = ApplyRequestWriter(path=path, now=lambda: 1234.0)

        writer.write()

        assert len(path.read_text(encoding="utf-8").splitlines()) == 1

    def test_상위_디렉터리가_없으면_만든다(self, tmp_path: Path) -> None:
        path = tmp_path / "없던" / "state" / "apply-request"
        writer = ApplyRequestWriter(path=path, now=lambda: 1.0)

        writer.write()

        assert path.exists()


class Test재기동_잠금:
    """mkdir 은 원자적이라 동시에 둘이 성공하지 못한다. apply.sh 의 LOCK 과 같다."""

    def test_처음_잡으면_성공한다(self, tmp_path: Path) -> None:
        lock = RestartLock(path=tmp_path / "apply.lock")
        assert lock.acquire() is True

    def test_이미_잡혀있으면_실패한다(self, tmp_path: Path) -> None:
        path = tmp_path / "apply.lock"
        먼저 = RestartLock(path=path, now=lambda: 1000.0)
        먼저.acquire()

        나중 = RestartLock(path=path, now=lambda: 1000.0)
        assert 나중.acquire() is False

    def test_풀면_다시_잡을_수_있다(self, tmp_path: Path) -> None:
        path = tmp_path / "apply.lock"
        lock = RestartLock(path=path)
        lock.acquire()

        lock.release()

        assert RestartLock(path=path).acquire() is True

    def test_묵은_잠금은_걷어내고_다시_잡는다(self, tmp_path: Path) -> None:
        """끊긴 적용이 남긴 잠금은 20분(stale_after_sec)이 지나면 죽은 것으로 본다."""
        path = tmp_path / "apply.lock"
        시계 = Clock(0.0)
        RestartLock(path=path, now=시계, stale_after_sec=1200.0).acquire()

        시계.value = 1200.0
        나중 = RestartLock(path=path, now=시계, stale_after_sec=1200.0)
        assert 나중.acquire() is True

    def test_묵지_않은_잠금은_그대로_거부한다(self, tmp_path: Path) -> None:
        path = tmp_path / "apply.lock"
        시계 = Clock(0.0)
        RestartLock(path=path, now=시계, stale_after_sec=1200.0).acquire()

        시계.value = 500.0
        나중 = RestartLock(path=path, now=시계, stale_after_sec=1200.0)
        assert 나중.acquire() is False


class TestPID교체_판정:
    """살아 있다는 것과 바뀌었다는 것은 다르다. 2026-09-22 22:02 구치파치가
    10시간 전 코드를 계속 돌리며 재기동 완료로 보고한 사례를 되풀이하지 않는다."""

    def test_새_PID가_살아있으면_성공이다(self) -> None:
        assert restart_confirmed(pid_before="111", pid_after="222", alive=lambda pid: pid == "222") is True

    def test_PID가_그대로면_실패다(self) -> None:
        """기동 시도가 조용히 아무 일도 안 한 경우 — 기존 프로세스가 살아 있어도 실패다."""
        assert restart_confirmed(pid_before="111", pid_after="111", alive=lambda pid: True) is False

    def test_새_PID가_죽어있으면_실패다(self) -> None:
        assert restart_confirmed(pid_before="111", pid_after="222", alive=lambda pid: False) is False

    def test_PID파일이_비어있으면_실패다(self) -> None:
        """봇이 아예 못 떴을 때 — 갈아탈 대상 자체가 없다."""
        assert restart_confirmed(pid_before="111", pid_after=None, alive=lambda pid: True) is False

    def test_원래_내려가_있었으면_새로_뜬_것만_보면_된다(self) -> None:
        """before 가 없던 경우(처음부터 내려가 있었음)는 교체 비교가 필요 없다."""
        assert restart_confirmed(pid_before=None, pid_after="222", alive=lambda pid: True) is True


class Test롤백_분기:
    """재기동 실패 후 롤백 여부는 생존 여부로 가른다 (G49)."""

    def test_재기동에_성공하면_롤백하지_않는다(self) -> None:
        assert should_rollback(restarted=True, process_running=True) is False

    def test_실패했지만_기존_프로세스가_살아있으면_롤백하지_않는다(self) -> None:
        """멀쩡히 도는 것을 되돌릴 이유가 없다 — 새 코드가 나쁘다는 증거가 아니다."""
        assert should_rollback(restarted=False, process_running=True) is False

    def test_실패했고_완전히_내려갔으면_롤백한다(self) -> None:
        assert should_rollback(restarted=False, process_running=False) is True


class Test네트워크장애_진단캡처_판정:
    """DNS/HTTPS 프로브가 3분(기본값) 연속 실패하면 그때의 네트워크 상태를
    진단으로 남긴다. 장애가 길어지면 한 시간마다 다시 남긴다 (G57)."""

    def test_capture_after_sec_전에는_캡처하지_않는다(self) -> None:
        escalation = NetworkOutageEscalation(capture_after_sec=180.0)
        assert escalation.needs_capture(elapsed_sec=100.0, already_captured=False) is False

    def test_capture_after_sec을_넘기면_처음_한_번_캡처한다(self) -> None:
        escalation = NetworkOutageEscalation(capture_after_sec=180.0)
        assert escalation.needs_capture(elapsed_sec=180.0, already_captured=False) is True

    def test_이미_캡처했으면_재포착_간격_전에는_다시_하지_않는다(self) -> None:
        escalation = NetworkOutageEscalation(capture_after_sec=180.0, recapture_interval_sec=3600.0)
        assert (
            escalation.needs_capture(elapsed_sec=1000.0, already_captured=True, since_last_capture_sec=100.0) is False
        )

    def test_재포착_간격이_지나면_다시_캡처한다(self) -> None:
        escalation = NetworkOutageEscalation(capture_after_sec=180.0, recapture_interval_sec=3600.0)
        assert (
            escalation.needs_capture(elapsed_sec=4000.0, already_captured=True, since_last_capture_sec=3600.0) is True
        )


class Test반복실패_재알림:
    """복구 실패가 반복될 때 1시간 간격으로만 재알림한다 (G54). 매 회차마다
    알리면 자동 복구가 도는 내내 같은 말을 되풀이해서 보낸다."""

    def test_처음은_바로_알린다(self) -> None:
        gate = RealertGate(interval_sec=3600.0)
        assert gate.due(now=1000.0) is True

    def test_알린_직후에는_다시_알리지_않는다(self) -> None:
        gate = RealertGate(interval_sec=3600.0)
        gate.mark_alerted(now=1000.0)
        assert gate.due(now=1500.0) is False

    def test_간격이_지나면_다시_알린다(self) -> None:
        gate = RealertGate(interval_sec=3600.0)
        gate.mark_alerted(now=1000.0)
        assert gate.due(now=1000.0 + 3600.0) is True

    def test_복구되면_되돌려_다음_장애에서_다시_바로_알린다(self) -> None:
        gate = RealertGate(interval_sec=3600.0)
        gate.mark_alerted(now=1000.0)
        gate.reset()
        assert gate.due(now=1500.0) is True


class Test미완료작업_표식:
    """봇이 일을 하던 중에 내려가면 복귀할 때 그 일을 알려야 한다. 그렇지 않으면
    요청자는 자기 요청이 조용히 사라졌다는 것을 모른다."""

    def test_표식이_없으면_peek은_None이다(self, tmp_path: Path) -> None:
        marker = PendingWorkMarker(path=tmp_path / "pending-work.json")
        assert marker.peek() is None

    def test_시작하면_내용을_읽을_수_있다(self, tmp_path: Path) -> None:
        marker = PendingWorkMarker(path=tmp_path / "pending-work.json", now=lambda: 500.0)

        marker.start("배포 스크립트 점검")

        pending = marker.peek()
        assert pending == PendingWork(description="배포 스크립트 점검", started_at=500.0)

    def test_끝나면_지워져서_peek은_다시_None이다(self, tmp_path: Path) -> None:
        marker = PendingWorkMarker(path=tmp_path / "pending-work.json")
        marker.start("어떤 작업")

        marker.clear()

        assert marker.peek() is None

    def test_깨진_파일은_없는_것으로_본다(self, tmp_path: Path) -> None:
        path = tmp_path / "pending-work.json"
        path.write_text("{ 이것은 JSON 이 아니다")
        marker = PendingWorkMarker(path=path)

        assert marker.peek() is None

    def test_상위_디렉터리가_없으면_만든다(self, tmp_path: Path) -> None:
        path = tmp_path / "없던" / "디렉터리" / "pending-work.json"
        marker = PendingWorkMarker(path=path)

        marker.start("작업")

        assert path.exists()


class Test복구보고_조립:
    """재기동 사유만으로는 중단된 작업이나 네트워크 장애 기간이 드러나지 않는다.
    둘 다 있으면 복구 보고 본문에 함께 실린다 (G59)."""

    def test_사유만_있으면_한_줄이다(self) -> None:
        text = assemble_recovery_report(reason="소켓 오류 9건", now=1000.0)
        assert text == "- 사유 : 소켓 오류 9건"

    def test_중단된_작업이_있으면_경과분과_함께_실린다(self) -> None:
        pending = PendingWork(description="배포 스크립트 점검", started_at=1000.0 - 120.0)

        text = assemble_recovery_report(reason="자동 재기동", now=1000.0, pending=pending)

        assert "배포 스크립트 점검" in text
        assert "2분" in text

    def test_네트워크_장애_기간도_실린다(self) -> None:
        text = assemble_recovery_report(reason="DNS 장애", now=1000.0, network_outage_sec=600.0)
        assert "10분" in text

    def test_둘_다_있으면_둘_다_실린다(self) -> None:
        pending = PendingWork(description="배포 점검", started_at=1000.0 - 60.0)

        text = assemble_recovery_report(reason="네트워크 장애", now=1000.0, pending=pending, network_outage_sec=900.0)

        assert "배포 점검" in text
        assert "15분" in text  # 900초 = 15분, 네트워크 장애 기간


class Test재기동_흐름_결합:
    """표식 작성 -> PID 교체 판정 -> 롤백 분기 -> 복구 보고까지 전체 경로를
    가짜로 한 번 이어 돈다."""

    def test_재기동_성공시_표식에서_보고까지(self, tmp_path: Path) -> None:
        req_path = tmp_path / "apply-request"
        writer = ApplyRequestWriter(path=req_path, now=lambda: 1000.0)
        writer.write(turn_id="T1")
        assert req_path.exists()

        confirmed = restart_confirmed(pid_before="100", pid_after="200", alive=lambda pid: True)
        assert confirmed is True
        assert should_rollback(restarted=confirmed, process_running=True) is False

        report = assemble_recovery_report(reason="코드 적용", now=1050.0)
        assert "코드 적용" in report

    def test_재기동_실패하고_완전히_내려가면_롤백한다(self) -> None:
        confirmed = restart_confirmed(pid_before="100", pid_after=None, alive=lambda pid: False)
        assert confirmed is False
        assert should_rollback(restarted=confirmed, process_running=False) is True


def test_pending_work와_JSON_직렬화가_서로_호환된다(tmp_path: Path) -> None:
    """PendingWorkMarker 가 쓰는 포맷을 다른 프로세스가 그대로 읽을 수 있어야 한다."""
    path = tmp_path / "pending-work.json"
    marker = PendingWorkMarker(path=path, now=lambda: 42.0)
    marker.start("점검")

    raw = json.loads(path.read_text())

    assert raw == {"description": "점검", "started_at": 42.0}


class Test재기동요청읽기:
    """감시기가 집기 전(apply-request)과 집은 뒤(apply-running) 표식을 같은 형식으로 읽는다."""

    def test_둘째줄의_턴을_채널과_ts로_나눈다(self) -> None:
        from slack_cli_agent.reliability.restart import ApplyRequest

        req = ApplyRequest.parse("1000\nC1:5.0\n")
        assert req is not None and req.requested_at == 1000.0
        assert req.requester() == ("C1", "5.0")

    def test_unknown_이나_빈_턴은_요청자가_없다(self) -> None:
        from slack_cli_agent.reliability.restart import ApplyRequest

        assert ApplyRequest.parse("1000\nunknown\n").requester() is None  # type: ignore[union-attr]
        assert ApplyRequest.parse("1000\n").requester() is None  # type: ignore[union-attr]

    def test_첫줄이_시각이_아니면_요청이_아니다(self) -> None:
        from slack_cli_agent.reliability.restart import ApplyRequest

        assert ApplyRequest.parse("") is None
        assert ApplyRequest.parse("abc\nC1:5.0\n") is None

    def test_기동_전에_남은_표식은_무시한다(self, tmp_path: Path) -> None:
        """새 프로세스가 이미 처리된 요청을 또 따르면 재기동이 되풀이된다."""
        from slack_cli_agent.reliability.restart import ApplyRequestWatch

        req_path = tmp_path / "apply-request"
        ApplyRequestWriter(path=req_path, now=lambda: 999.0).write("C1:5.0")
        watch = ApplyRequestWatch(paths=[req_path], started_at=1000.5)
        assert watch.pending() is None
        ApplyRequestWriter(path=req_path, now=lambda: 1000.9).write("C1:5.0")
        assert watch.pending() is not None

    def test_감시기가_집은_표식도_요청으로_본다(self, tmp_path: Path) -> None:
        from slack_cli_agent.reliability.restart import ApplyRequestWatch

        running = tmp_path / "apply-running"
        ApplyRequestWriter(path=running, now=lambda: 2000.0).write()
        watch = ApplyRequestWatch(paths=[tmp_path / "apply-request", running], started_at=1000.0)
        assert watch.pending() is not None


class Test재기동요청_워커_유예:
    """apply-request 표식이 있으면 새 작업을 집지 않고, 진행 중 작업이 끝나면
    재기동 종료 코드로 나온다. 표식을 남긴 턴은 30초 뒤 기다리지 않고 다음
    기동에서 다시 돌리지 않는다 (마메치 bot.py on_terminate)."""

    def _worker(self, database, handler, request_box: list, max_concurrent: int = 2):
        import time as _time

        from test_worker import make_worker

        from slack_cli_agent.config.settings import RuntimeSettings

        # 유예와 종료 대기는 실제 시간으로 잰다. 요청한 턴의 유예만 짧게 줄인다.
        worker, queue, client = make_worker(
            database=database,
            handler=handler,
            settings=RuntimeSettings(
                heartbeat_interval_sec=0.01, max_concurrent=max_concurrent,
                queue_idle_sleep_sec=0.01, shutdown_grace_sec=3.0,
            ),
            sleep=_time.sleep,
        )
        worker.watch_restart_requests(
            lambda: request_box[0] if request_box else None, requester_grace_sec=0.1
        )
        return worker, queue, client

    def test_표식이_있으면_새_작업을_집지_않는다(self, database) -> None:
        from test_worker import FakeHandler, ctx

        from slack_cli_agent.reliability.restart import RESTART_EXIT_CODE, ApplyRequest

        for slots in (1, 2):
            handler = FakeHandler()
            worker, queue, _ = self._worker(
                database, handler, [ApplyRequest(requested_at=1.0)], max_concurrent=slots
            )
            queue.enqueue(ctx(f"{slots}.0", thread=f"t{slots}"))
            worker.run_forever(lambda: False)
            assert handler.received == []
            assert worker.exit_code == RESTART_EXIT_CODE
            assert f"{slots}.0" in [j.context.ts for j in queue.pending()]

    def test_진행_중_작업이_끝난_뒤에_나온다(self, database) -> None:
        import time as _time

        from test_worker import ctx

        from slack_cli_agent.core.ports import HandleOutcome
        from slack_cli_agent.reliability.restart import RESTART_EXIT_CODE, ApplyRequest

        box: list = []
        끝: list[str] = []
        holder: dict = {}

        class 표식남기는핸들러:
            def handle(self, context):
                if context.ts == "1.0":
                    box.append(ApplyRequest(requested_at=1.0, turn_id="C1:다른턴"))
                    holder["queue"].enqueue(ctx("2.0", thread="t2"))
                    _time.sleep(0.2)
                끝.append(context.ts)
                return HandleOutcome(ok=True)

        worker, queue, _ = self._worker(database, 표식남기는핸들러(), box)
        holder["queue"] = queue
        queue.enqueue(ctx("1.0", thread="t1"))
        worker.run_forever(lambda: False)
        assert 끝 == ["1.0"]
        assert worker.exit_code == RESTART_EXIT_CODE
        assert [j.context.ts for j in queue.pending()] == ["2.0"]

    def test_요청한_턴은_유예_뒤_기다리지_않고_다시_돌리지_않는다(self, database) -> None:
        from test_worker import BlockingHandler, ctx

        from slack_cli_agent.reliability.restart import ApplyRequest

        handler = BlockingHandler()
        box: list = []
        worker, queue, client = self._worker(database, handler, box)
        queue.enqueue(ctx("5.0", thread="t5"))
        멈춤 = {"n": 0}

        def should_stop() -> bool:
            if handler.entered_event.is_set() and not box:
                box.append(ApplyRequest(requested_at=1.0, turn_id="C1:5.0"))
            멈춤["n"] += 1
            return 멈춤["n"] > 100000

        try:
            worker.run_forever(should_stop)
            # 막힌 채로 돌아왔다 : 요청한 턴을 끝까지 기다리지 않았다.
            assert not handler.release_event.is_set()
            assert worker.restart_requester_discount(0.0) == 0
            assert worker.restart_requester_discount(0.1) == 1
            worker.shutdown()
            # 다시 큐로 돌리지 않는다. 돌리면 같은 턴이 또 재기동을 예약한다.
            assert queue.pending() == []
            assert ("add", "C1", "5.0", "hourglass") not in client.calls
        finally:
            handler.release()

    def test_TERM_이_먼저_와도_요청한_턴을_기다리지_않고_다시_돌리지_않는다(self, database) -> None:
        """감시기의 kickstart 가 워커가 표식을 보기 전에 닿는 경우다."""
        from test_worker import BlockingHandler, ctx

        from slack_cli_agent.reliability.restart import ApplyRequest

        handler = BlockingHandler()
        box: list = []
        worker, queue, _client = self._worker(database, handler, box)
        queue.enqueue(ctx("6.0", thread="t6"))

        def should_stop() -> bool:
            if handler.entered_event.is_set():
                box.append(ApplyRequest(requested_at=1.0, turn_id="C1:6.0"))
                return True
            return False

        try:
            시작 = time.monotonic()
            worker.run_forever(should_stop)
            # 처리기는 5초 동안 막혀 있다. 유예 0.1초 뒤 돌아와야 한다.
            assert time.monotonic() - 시작 < 2.0
            assert worker.exit_code == 0
            worker.shutdown()
            assert queue.pending() == []
        finally:
            handler.release()

    def test_주입이_없으면_종료_코드는_0이다(self, database) -> None:
        from test_worker import make_worker

        worker, _q, _c = make_worker(database=database)
        assert worker.exit_code == 0
        assert worker.restart_requester_discount(100.0) == 0


def test_종료_대기는_요청한_턴을_빼고_센다() -> None:
    """launchctl kickstart 의 TERM 도 요청한 턴을 330초 기다리지 않는다."""
    from slack_cli_agent.core.lifecycle import GracefulShutdown, InflightCounter

    counter = InflightCounter()
    counter.enter()
    waits: list[float] = []
    완료: list[bool] = []
    shutdown = GracefulShutdown(
        counter,
        330,
        signal_register=lambda s, h: None,
        sleep_fn=waits.append,
        discount=lambda waited: 1 if waited >= 30 else 0,
        on_shutdown_complete=lambda: 완료.append(True),
    )
    shutdown.trigger()
    assert sum(waits) == 30
    assert 완료 == [True]
