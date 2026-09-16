"""진행 표시를 실제로 내보내는 계층 — 훅 기록, 폴링 세션, 슬랙 표시, 조립."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from slack_cli_agent.config.channel import ChannelConfig
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.observability.progress import (
    IDLE_TEXT,
    START_TEXT,
    ProgressCoordinator,
    ProgressSession,
    ProgressTracker,
)
from slack_cli_agent.observability.progress_hook import record
from slack_cli_agent.slack.progress import MAX_LINES, SlackProgressSink


class FakeSink:
    def __init__(self) -> None:
        self.opened: list[str] = []
        self.lines: list[str] = []
        self.closed = 0

    def open(self, text: str) -> None:
        self.opened.append(text)

    def append(self, lines: Any) -> None:
        self.lines.extend(lines)

    def close(self) -> None:
        self.closed += 1


class RaisingSink(FakeSink):
    def open(self, text: str) -> None:
        raise RuntimeError("열기 실패")

    def append(self, lines: Any) -> None:
        raise RuntimeError("갱신 실패")

    def close(self) -> None:
        raise RuntimeError("닫기 실패")


class TestProgressHook:
    def test_도구_이름을_한_줄로_덧붙인다(self, tmp_path: Path) -> None:
        log_path = tmp_path / "s.log"
        record(log_path, json.dumps({"tool_name": "Read"}))
        record(log_path, json.dumps({"tool_name": "Bash"}))
        assert [json.loads(line)["tool"] for line in log_path.read_text().splitlines()] == [
            "Read", "Bash",
        ]

    def test_도구_이름이_없으면_아무것도_안_쓴다(self, tmp_path: Path) -> None:
        log_path = tmp_path / "s.log"
        record(log_path, json.dumps({"session_id": "abc"}))
        assert not log_path.exists()

    def test_깨진_입력에도_예외를_내지_않는다(self, tmp_path: Path) -> None:
        # 훅이 실패하면 엔진이 그것을 도구 호출 오류로 답변에 싣는다
        record(tmp_path / "s.log", "json 이 아니다")

    def test_쓸_수_없는_경로에도_예외를_내지_않는다(self, tmp_path: Path) -> None:
        record(tmp_path / "없는디렉터리" / "s.log", json.dumps({"tool_name": "Read"}))


class TestProgressSession:
    def _session(self, sink: Any, log_path: Path, tick: float = 0.01) -> ProgressSession:
        settings = RuntimeSettings(progress_tick_sec=tick, progress_idle_sec=1000)
        return ProgressSession(ProgressTracker(settings), sink, log_path, tick)

    def _wait_for(self, predicate: Any, timeout: float = 2.0) -> bool:
        deadline = threading.Event()
        for _ in range(int(timeout / 0.01)):
            if predicate():
                return True
            deadline.wait(0.01)
        return predicate()

    def test_시작하면_시작_문구를_내보낸다(self, tmp_path: Path) -> None:
        sink = FakeSink()
        session = self._session(sink, tmp_path / "s.log")
        session.start()
        session.stop()
        assert sink.opened == [START_TEXT]
        assert sink.closed == 1

    def test_이전_요청의_줄을_지우고_시작한다(self, tmp_path: Path) -> None:
        # 세션을 이어가면 훅이 쓴 지난 요청의 줄이 파일에 남아 있다
        log_path = tmp_path / "s.log"
        log_path.write_text(json.dumps({"tool": "Read"}) + "\n", encoding="utf-8")
        session = self._session(FakeSink(), log_path)
        session.start()
        assert log_path.read_text() == ""
        session.stop()

    def test_훅이_쓴_줄이_단계_문구로_나간다(self, tmp_path: Path) -> None:
        log_path = tmp_path / "s.log"
        sink = FakeSink()
        session = self._session(sink, log_path)
        session.start()
        record(log_path, json.dumps({"tool_name": "Bash"}))
        assert self._wait_for(lambda: sink.lines == ["명령 실행 중"])
        session.stop()

    def test_끝나면_로그_파일을_지운다(self, tmp_path: Path) -> None:
        log_path = tmp_path / "s.log"
        session = self._session(FakeSink(), log_path)
        session.start()
        session.stop()
        assert not log_path.exists()

    def test_표시가_실패해도_예외가_나가지_않는다(self, tmp_path: Path) -> None:
        # 표시는 덤이다. 답이 나온 요청을 표시 실패로 죽이면 안 된다
        session = self._session(RaisingSink(), tmp_path / "s.log")
        session.start()
        session.stop()


class TestProgressCoordinator:
    def _coordinator(self, tmp_path: Path, sink: Any) -> ProgressCoordinator:
        return ProgressCoordinator(
            settings=RuntimeSettings(progress_tick_sec=0.01),
            sink_factory=lambda channel, thread_ts: sink,
            log_dir=tmp_path,
        )

    def test_진행_표시가_꺼진_채널은_로그_경로가_없다(self, tmp_path: Path) -> None:
        coordinator = self._coordinator(tmp_path, FakeSink())
        config = ChannelConfig.from_dict("C1", {"progress": False})
        assert coordinator.log_path_for(config, "C1", "1.0") is None

    def test_설정이_없는_채널도_로그_경로가_없다(self, tmp_path: Path) -> None:
        coordinator = self._coordinator(tmp_path, FakeSink())
        assert coordinator.log_path_for(None, "C1", "1.0") is None

    def test_켜진_채널은_요청마다_다른_경로를_준다(self, tmp_path: Path) -> None:
        coordinator = self._coordinator(tmp_path, FakeSink())
        config = ChannelConfig.from_dict("C1", {"progress": True})
        first = coordinator.log_path_for(config, "C1", "1.0")
        second = coordinator.log_path_for(config, "C1", "2.0")
        assert first is not None and second is not None and first != second

    def test_로그_경로가_없으면_표시를_열지_않는다(self, tmp_path: Path) -> None:
        sink = FakeSink()
        coordinator = self._coordinator(tmp_path, sink)
        with coordinator.session("C1", "1.0", None):
            pass
        assert sink.opened == []

    def test_로그_경로가_있으면_구간_동안_표시가_열린다(self, tmp_path: Path) -> None:
        sink = FakeSink()
        coordinator = self._coordinator(tmp_path, sink)
        with coordinator.session("C1", "1.0", tmp_path / "s.log"):
            assert sink.opened == [START_TEXT]
            assert sink.closed == 0
        assert sink.closed == 1

    def test_구간_안에서_예외가_나도_표시를_닫는다(self, tmp_path: Path) -> None:
        sink = FakeSink()
        coordinator = self._coordinator(tmp_path, sink)
        with pytest.raises(RuntimeError), coordinator.session("C1", "1.0", tmp_path / "s.log"):
            raise RuntimeError("엔진 실패")
        assert sink.closed == 1


class FakeClient:
    def __init__(self, fail_update: bool = False) -> None:
        self.posted: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.deleted: list[dict[str, Any]] = []
        self._fail_update = fail_update

    def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.posted.append(kwargs)
        return {"ts": "111.222"}

    def chat_update(self, **kwargs: Any) -> dict[str, str]:
        if self._fail_update:
            raise RuntimeError("갱신 거부")
        self.updated.append(kwargs)
        return {"ts": kwargs["ts"]}

    def chat_delete(self, **kwargs: Any) -> dict[str, str]:
        self.deleted.append(kwargs)
        return {"ts": kwargs["ts"]}


class TestSlackProgressSink:
    def test_스레드에_한_건만_올리고_같은_건을_고쳐_쓴다(self) -> None:
        client = FakeClient()
        sink = SlackProgressSink(client, "C1", "1.0", "이카리 신지")
        sink.open(START_TEXT)
        sink.append(["파일 읽는 중"])
        sink.append(["명령 실행 중"])
        assert len(client.posted) == 1
        assert client.posted[0]["thread_ts"] == "1.0"
        assert [call["ts"] for call in client.updated] == ["111.222", "111.222"]
        assert client.updated[-1]["text"] == f"{START_TEXT}\n파일 읽는 중\n명령 실행 중"

    def test_DM_은_스레드로_달지_않는다(self) -> None:
        # 게시 계층과 같은 규칙이다. DM 에는 답을 달 스레드가 없다
        client = FakeClient()
        SlackProgressSink(client, "D1", "1.0").open(START_TEXT)
        assert "thread_ts" not in client.posted[0]

    def test_줄이_너무_많아지면_오래된_것부터_버린다(self) -> None:
        client = FakeClient()
        sink = SlackProgressSink(client, "C1", "1.0")
        sink.open(START_TEXT)
        sink.append([f"단계 {i}" for i in range(MAX_LINES + 10)])
        text = client.updated[-1]["text"]
        assert len(text.splitlines()) == MAX_LINES
        assert START_TEXT not in text

    def test_끝나면_표시를_지운다(self) -> None:
        client = FakeClient()
        sink = SlackProgressSink(client, "C1", "1.0")
        sink.open(START_TEXT)
        sink.close()
        assert client.deleted == [{"channel": "C1", "ts": "111.222"}]

    def test_두_번_닫아도_한_번만_지운다(self) -> None:
        client = FakeClient()
        sink = SlackProgressSink(client, "C1", "1.0")
        sink.open(START_TEXT)
        sink.close()
        sink.close()
        assert len(client.deleted) == 1

    def test_열지_못했으면_갱신도_삭제도_하지_않는다(self) -> None:
        client = FakeClient()
        sink = SlackProgressSink(client, "C1", "1.0")
        sink.append([IDLE_TEXT])
        sink.close()
        assert client.updated == [] and client.deleted == []
