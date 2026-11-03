"""재기동 위임, PID교체 판정, 롤백 분기, 네트워크 장애 감시, 미완료 보고.

~/Projects/mametchi-slack-bot 의 apply.sh/apply-restart.sh/restart.sh/
watchdog.py 를 코드로만 확인해 기대값을 정했다 (실행 없이 순수 로직만 뽑음).
turn 의 Seatbelt 샌드박스는 launchctl kickstart 를 거부하므로, 봇 턴은
재기동을 직접 하지 않고 요청 표식만 남긴다. 실제 재기동/프로세스 종료/
네트워크 호출은 하지 않는다 — 전부 주입한 가짜로 돈다.
"""

from __future__ import annotations

import json
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
