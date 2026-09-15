"""상태 스냅샷 기록 시험.

원본 `bot.py` 의 `state_snapshot()`/`write_state_snapshot()` 을 이관한다.
프로세스 메모리에만 있는 값(대기줄, 소켓 오류 이력, 캐치업 대기)을 파일로
내려 적어 밖에서 볼 수 있게 하는 기능이다. 원본은 전역 변수를 직접 읽었으나
여기서는 값을 제공하는 쪽을 `SnapshotSource` 뒤로 감춰 주입받는다.
"""

from __future__ import annotations

import json

from slack_cli_agent.observability.state_snapshot import (
    StateSnapshotBuilder,
    StateSnapshotWriter,
)


class FakeSource:
    """시험용 값 제공자. 필드를 자유롭게 바꿔가며 빌더 동작을 본다."""

    def __init__(self) -> None:
        self.inflight = 0
        self.queued = {}
        self.socket_errors = []
        self.socket_reconnects = []
        self.catchup_pending = 0
        self.watch_jobs: int | None = 0
        self.shutting_down = False
        self._started_at = 1_000.0

    def inflight_count(self) -> int:
        return self.inflight

    def queued_threads(self):
        return dict(self.queued)

    def socket_error_timestamps(self):
        return list(self.socket_errors)

    def socket_reconnect_timestamps(self):
        return list(self.socket_reconnects)

    def catchup_pending_count(self) -> int:
        return self.catchup_pending

    def watch_job_count(self):
        return self.watch_jobs

    def is_shutting_down(self) -> bool:
        return self.shutting_down

    def started_at(self) -> float:
        return self._started_at


class RaisingSource(FakeSource):
    """값을 모으는 도중 예외를 낸다."""

    def inflight_count(self) -> int:
        raise RuntimeError("값 조회 실패")


class TestStateSnapshotBuilder:
    def test_기본_필드가_모두_담긴다(self) -> None:
        source = FakeSource()
        source.inflight = 2
        source.catchup_pending = 5
        source.watch_jobs = 7
        builder = StateSnapshotBuilder(source, now=lambda: 1_100.0, pid=lambda: 4321)
        snap = builder.build()
        assert snap["pid"] == 4321
        assert snap["written_at"] == 1_100.0
        assert snap["started_at"] == 1_000.0
        assert snap["uptime_sec"] == 100.0
        assert snap["inflight"] == 2
        assert snap["catchup_pending"] == 5
        assert snap["watch_jobs"] == 7
        assert snap["shutting_down"] is False

    def test_대기줄에서_빈_항목은_제외되고_합계가_계산된다(self) -> None:
        source = FakeSource()
        source.queued = {"T1": 3, "T2": 0, "T3": 2}
        builder = StateSnapshotBuilder(source, now=lambda: 0.0, pid=lambda: 1)
        snap = builder.build()
        assert snap["queued"] == {"T1": 3, "T3": 2}
        assert snap["queued_threads"] == 2
        assert snap["queued_total"] == 5

    def test_최근_3분_소켓_오류만_따로_집계된다(self) -> None:
        source = FakeSource()
        now = 1_000.0
        source.socket_errors = [now - 500, now - 100, now - 10]
        source.socket_reconnects = [now - 400, now - 5]
        builder = StateSnapshotBuilder(source, now=lambda: now, pid=lambda: 1)
        snap = builder.build()
        assert snap["socket_errors_total"] == 3
        assert snap["socket_errors_3min"] == 2
        assert snap["socket_reconnects_total"] == 2
        assert snap["socket_reconnects_3min"] == 1

    def test_watch_jobs가_None이면_그대로_담긴다(self) -> None:
        source = FakeSource()
        source.watch_jobs = None
        builder = StateSnapshotBuilder(source, now=lambda: 0.0, pid=lambda: 1)
        snap = builder.build()
        assert snap["watch_jobs"] is None

    def test_종료_중_상태가_반영된다(self) -> None:
        source = FakeSource()
        source.shutting_down = True
        builder = StateSnapshotBuilder(source, now=lambda: 0.0, pid=lambda: 1)
        snap = builder.build()
        assert snap["shutting_down"] is True


class TestStateSnapshotWriter:
    def test_파일에_json으로_기록된다(self, tmp_path) -> None:
        source = FakeSource()
        source.inflight = 1
        path = tmp_path / "state.json"
        writer = StateSnapshotWriter(path, StateSnapshotBuilder(source, now=lambda: 1.0, pid=lambda: 99))
        writer.write()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["inflight"] == 1
        assert data["pid"] == 99

    def test_임시_파일이_남지_않는다(self, tmp_path) -> None:
        source = FakeSource()
        path = tmp_path / "state.json"
        writer = StateSnapshotWriter(path, StateSnapshotBuilder(source))
        writer.write()
        leftovers = [p for p in tmp_path.iterdir() if p.name != "state.json"]
        assert leftovers == []

    def test_여러_번_써도_최신값으로_갈아_끼워진다(self, tmp_path) -> None:
        source = FakeSource()
        path = tmp_path / "state.json"
        writer = StateSnapshotWriter(path, StateSnapshotBuilder(source, now=lambda: 1.0, pid=lambda: 1))
        source.inflight = 1
        writer.write()
        source.inflight = 9
        writer.write()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["inflight"] == 9

    def test_값_수집_실패해도_예외가_밖으로_안_나간다(self, tmp_path) -> None:
        source = RaisingSource()
        path = tmp_path / "state.json"
        writer = StateSnapshotWriter(path, StateSnapshotBuilder(source))
        writer.write()  # 예외 없이 끝나야 한다
        assert not path.exists()

    def test_쓰기_실패해도_예외가_밖으로_안_나간다(self, tmp_path) -> None:
        source = FakeSource()
        # 존재하지 않는 디렉터리를 대상으로 지정해 쓰기 자체가 실패하게 만든다.
        path = tmp_path / "no-such-dir" / "state.json"
        writer = StateSnapshotWriter(path, StateSnapshotBuilder(source))
        writer.write()  # 예외 없이 끝나야 한다
