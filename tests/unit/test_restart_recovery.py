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


def _지금() -> float:
    return float(int(time.time()))


def _steps(n: int):
    """n 번째 호출부터 멈추라고 답하는 should_stop."""
    count = {"n": 0}

    def should_stop() -> bool:
        count["n"] += 1
        return count["n"] > n

    return should_stop


class Test재기동요청_워커_유예:
    """apply-request 표식이 있으면 새 작업을 집지 않고 감시기의 TERM 을 기다린다.
    워커가 스스로 나가면 감시기의 kickstart 가 한 번 더 재기동한다. 표식을 남긴
    턴은 30초 뒤 기다리지 않고 다음 기동에서 다시 돌리지 않는다 (마메치 bot.py
    on_terminate, watchdog.py take_over_sca_apply)."""

    def _worker(self, database, handler, request_box: list, max_concurrent: int = 2, **kw):
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
            sleep=time.sleep,
        )
        kw.setdefault("requester_grace_sec", 0.1)
        worker.watch_restart_requests(lambda: request_box[0] if request_box else None, **kw)
        return worker, queue, client

    def test_표식이_있으면_새_작업을_집지_않고_스스로_나가지도_않는다(self, database) -> None:
        from test_worker import FakeHandler, ctx

        from slack_cli_agent.reliability.restart import ApplyRequest

        for slots in (1, 2):
            handler = FakeHandler()
            worker, queue, _ = self._worker(
                database, handler, [ApplyRequest(requested_at=_지금())], max_concurrent=slots
            )
            queue.enqueue(ctx(f"{slots}.0", thread=f"t{slots}"))
            stop = _steps(30)
            worker.run_forever(stop)
            assert handler.received == []
            # should_stop 이 멈추라고 할 때까지 돌았다 : 표식만으로 돌아오지 않는다.
            assert stop() is True
            assert f"{slots}.0" in [j.context.ts for j in queue.pending()]
            assert not hasattr(worker, "exit_code")

    def test_표식이_사라지면_다시_집는다(self, database) -> None:
        from test_worker import FakeHandler, ctx

        from slack_cli_agent.reliability.restart import ApplyRequest

        handler = FakeHandler()
        box: list = [ApplyRequest(requested_at=_지금())]
        worker, queue, _ = self._worker(database, handler, box)
        queue.enqueue(ctx("1.0", thread="t1"))
        assert worker.holding_claims()
        box.clear()
        worker.run_forever(_steps(30))
        assert [c.ts for c in handler.received] == ["1.0"]

    def test_아무도_집지_않는_표식은_보류_한도_뒤_다시_집는다(self, database) -> None:
        """감시기가 없으면 보류가 끝나지 않아 모든 응답이 멈춘다."""
        from test_worker import FakeHandler, ctx

        from slack_cli_agent.reliability.restart import ApplyRequest

        handler = FakeHandler()
        box: list = [ApplyRequest(requested_at=_지금())]
        worker, queue, _ = self._worker(database, handler, box, hold_max_sec=0.1)
        queue.enqueue(ctx("1.0", thread="t1"))
        worker.run_forever(_steps(200))
        assert [c.ts for c in handler.received] == ["1.0"]

    def test_슬롯이_다_찬_동안_생긴_표식도_빈_슬롯에_새_작업을_넣지_않는다(self, database) -> None:
        import threading

        from test_worker import ctx

        from slack_cli_agent.core.ports import HandleOutcome
        from slack_cli_agent.reliability.restart import ApplyRequest

        box: list = []
        시작: list[str] = []
        풀기 = threading.Event()
        holder: dict = {}

        class 막는핸들러:
            def handle(self, context):
                시작.append(context.ts)
                if context.ts == "1.0":
                    # 두 슬롯이 다 찬 뒤 표식이 생기고 새 작업이 들어온다.
                    while len(시작) < 2:
                        time.sleep(0.01)
                    box.append(ApplyRequest(requested_at=_지금()))
                    holder["queue"].enqueue(ctx("3.0", thread="t3"))
                    return HandleOutcome(ok=True)
                풀기.wait(timeout=5)
                return HandleOutcome(ok=True)

        worker, queue, _ = self._worker(database, 막는핸들러(), box)
        holder["queue"] = queue
        queue.enqueue(ctx("1.0", thread="t1"))
        queue.enqueue(ctx("2.0", thread="t2"))
        threading.Timer(0.5, 풀기.set).start()
        worker.run_forever(_steps(100))
        assert sorted(시작) == ["1.0", "2.0"]
        assert [j.context.ts for j in queue.pending()] == ["3.0"]

    def test_종료할_때_요청한_턴이_아닌_작업은_끝까지_기다린다(self, database) -> None:
        """grace 가 지났다고 끊으면 슬랙 중간 게시와 파일 쓰기가 남은 채 다시 돈다."""
        from test_worker import ctx

        from slack_cli_agent.core.ports import HandleOutcome
        from slack_cli_agent.reliability.restart import ApplyRequest

        box: list = [ApplyRequest(requested_at=_지금() + 10, turn_id="C1:9.0")]
        끝: list[str] = []

        class 느린핸들러:
            def handle(self, context):
                time.sleep(0.5)
                끝.append(context.ts)
                return HandleOutcome(ok=True)

        worker, queue, _ = self._worker(database, 느린핸들러(), [])
        queue.enqueue(ctx("1.0", thread="t1"))
        진행 = {"n": 0}

        def should_stop() -> bool:
            진행["n"] += 1
            if 진행["n"] == 3:
                worker._restart_request = lambda: box[0]
                return True
            return False

        worker.run_forever(should_stop)
        assert 끝 == ["1.0"]

    def test_요청한_턴은_유예_뒤_기다리지_않고_다시_돌리지_않는다(self, database) -> None:
        from test_worker import BlockingHandler, ctx

        from slack_cli_agent.reliability.restart import ApplyRequest

        handler = BlockingHandler()
        box: list = []
        worker, queue, client = self._worker(database, handler, box)
        queue.enqueue(ctx("5.0", thread="t5"))

        def should_stop() -> bool:
            if handler.entered_event.is_set():
                if not box:
                    box.append(ApplyRequest(requested_at=_지금(), turn_id="C1:5.0"))
                return True
            return False

        try:
            시작 = time.monotonic()
            worker.run_forever(should_stop)
            # 처리기는 5초 동안 막혀 있다. 유예 0.1초 뒤 돌아와야 한다.
            assert time.monotonic() - 시작 < 2.0
            assert worker.restart_requester_discount(0.0) == 0
            assert worker.restart_requester_discount(0.1) == 1
            # 감시기가 kickstart 뒤 표식을 지워도 요청한 턴을 기억한다.
            box.clear()
            assert worker.restart_requester_discount(0.1) == 1
            worker.shutdown()
            # 다시 큐로 돌리지 않는다. 돌리면 같은 턴이 또 재기동을 예약한다.
            assert queue.pending() == []
            assert ("add", "C1", "5.0", "hourglass") not in client.calls
        finally:
            handler.release()

    def _요청자_판정(self, database, request, requester_ok=None) -> tuple[int, list]:
        from test_worker import BlockingHandler, ctx

        handler = BlockingHandler()
        box: list = []
        kw = {"requester_ok": requester_ok} if requester_ok else {}
        worker, queue, _ = self._worker(database, handler, box, **kw)
        queue.enqueue(ctx("7.0", thread="t7"))
        try:
            def should_stop() -> bool:
                if handler.entered_event.is_set():
                    box.append(request)
                    return True
                return False

            worker._requester_grace_sec = 0.0
            discount = None
            # 요청자가 아니면 끝까지 기다리므로 별도 스레드에서 돌린다.
            import threading

            t = threading.Thread(target=worker.run_forever, args=(should_stop,), daemon=True)
            t.start()
            handler.entered_event.wait(timeout=2)
            time.sleep(0.05)
            discount = worker.restart_requester_discount(1.0)
            worker.shutdown()
            return discount, [j.context.ts for j in queue.pending()]
        finally:
            handler.release()

    def test_표식보다_늦게_시작한_작업은_요청자로_보지_않는다(self, database) -> None:
        """표식에 적힌 턴이 표식을 쓴 턴인지 대조한다."""
        from slack_cli_agent.reliability.restart import ApplyRequest

        discount, pending = self._요청자_판정(
            database, ApplyRequest(requested_at=_지금() - 100, turn_id="C1:7.0")
        )
        assert discount == 0
        assert pending == ["7.0"]

    def test_요청자_허용_조건을_통과하지_못하면_요청자로_보지_않는다(self, database) -> None:
        """소유자가 아닌 사람의 요청을 다른 턴이 적은 표식으로 완료 처리하지 않는다."""
        from slack_cli_agent.reliability.restart import ApplyRequest

        request = ApplyRequest(requested_at=_지금() + 10, turn_id="C1:7.0")
        discount, pending = self._요청자_판정(
            database, request, requester_ok=lambda c: c.user == "U_OWNER"
        )
        assert discount == 0
        assert pending == ["7.0"]
        discount, pending = self._요청자_판정(
            database, request, requester_ok=lambda c: c.user == "U1"
        )
        assert discount == 1
        assert pending == []

    def test_메인_스레드가_잠금을_쥔_채_신호를_받아도_차감_판정이_멈추지_않는다(self, database) -> None:
        """TERM 처리기는 메인 스레드에서 돈다. 메인 스레드가 holding_claims 등으로
        워커 잠금을 쥔 구간에 신호가 오면 처리기가 같은 잠금을 다시 잡는다.
        재진입이 안 되는 잠금이면 SIGKILL 까지 멈춘다."""
        import threading

        from test_worker import make_worker

        from slack_cli_agent.reliability.restart import ApplyRequest

        worker, _q, _c = make_worker(database=database)
        worker.watch_restart_requests(
            lambda: ApplyRequest(requested_at=_지금(), turn_id="C1:5.0"),
            requester_grace_sec=0.0,
        )
        결과: list[int] = []

        def 신호_처리기_흉내() -> None:
            # 같은 스레드에서 잠금을 쥔 채 처리기가 끼어든 상황을 만든다.
            with worker._lock:
                결과.append(worker.restart_requester_discount(1.0))

        t = threading.Thread(target=신호_처리기_흉내, daemon=True)
        t.start()
        t.join(timeout=2)
        assert not t.is_alive(), "처리기가 자기 스레드가 쥔 잠금을 기다리며 멈췄다"
        assert 결과 == [0]

    def test_주입이_없으면_보류도_차감도_없다(self, database) -> None:
        from test_worker import make_worker

        worker, _q, _c = make_worker(database=database)
        assert worker.holding_claims() is False
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


class Test재기동요청_배선:
    """운영 경로에서 표식 둘째 줄의 턴 식별자가 실제로 채워지고, 감시 확인도
    보류된다. 시험이 turn_id 를 직접 넣으면 이 공백을 잡지 못한다."""

    def test_파이프라인이_턴_식별자를_엔진_환경에_싣는다(self, tmp_path: Path) -> None:
        from test_pipeline import build_pipeline, make_ctx, ok_response

        pipeline, deps = build_pipeline(responses=[ok_response()], tmp_path=tmp_path)
        context = make_ctx(channel="C1")
        pipeline.handle(context)

        env = deps["runner"].calls[0].engine_env
        assert env["TURN_ID"] == f"C1:{context.ts}"

    def test_세_엔진_모두_턴_식별자를_자식에_넘긴다(self) -> None:
        from slack_cli_agent.engine.environment import (
            ClaudeEnvironmentPolicy,
            CodexEnvironmentPolicy,
            GeminiEnvironmentPolicy,
        )

        for policy in (ClaudeEnvironmentPolicy, CodexEnvironmentPolicy, GeminiEnvironmentPolicy):
            env = policy("test").build({"PATH": "/bin", "TURN_ID": "C1:5.0"})
            assert env["TURN_ID"] == "C1:5.0", policy.__name__
            assert "TURN_ID" not in policy("test").build({"PATH": "/bin"})

    def test_재기동_안내에_둘째_줄_형식이_있다(self, monkeypatch) -> None:
        from slack_cli_agent.guard.pretool import restart_guard

        monkeypatch.setattr(restart_guard, "_MARKER_PATH", "/x/apply-request")
        hint = restart_guard._marker_hint()
        assert '"$TURN_ID"' in hint and "/x/apply-request" in hint

    def test_보류_중에는_감시_확인을_돌리지_않는다(self, tmp_path: Path, monkeypatch) -> None:
        from test_application import FakeSlackClient, write_profile

        from slack_cli_agent.core.application import Application

        app = Application.from_profile(write_profile(tmp_path), client=FakeSlackClient())
        checks: list[int] = []
        monkeypatch.setattr(
            type(app.watch_checker()), "check_once", lambda self: checks.append(1)
        )
        보류 = {"on": True}
        runner = app.watch_runner(hold=lambda: 보류["on"])
        runner._task()
        assert checks == []
        보류["on"] = False
        runner._task()
        assert checks == [1]
