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
from slack_cli_agent.slack.progress import (
    MAX_LINES,
    FallbackProgressSink,
    ProgressStreamUnavailable,
    SlackProgressSink,
    SlackStreamingProgressSink,
)


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
    def __init__(self, fail_update: bool = False, fail_stream: str = "") -> None:
        self.posted: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.deleted: list[dict[str, Any]] = []
        self.started: list[dict[str, Any]] = []
        self.appended: list[dict[str, Any]] = []
        self.stopped: list[dict[str, Any]] = []
        self._fail_update = fail_update
        # "" 면 스트리밍이 된다. 값이 있으면 그 이름의 호출에서만 거부한다 —
        # 시작에서 막히는 경우와 도중에 막히는 경우가 전환 경로가 다르다
        self._fail_stream = fail_stream

    def chat_startStream(self, **kwargs: Any) -> dict[str, str]:
        if self._fail_stream == "start":
            raise RuntimeError("스트리밍 권한 없음")
        self.started.append(kwargs)
        return {"ts": "333.444"}

    def chat_appendStream(self, **kwargs: Any) -> dict[str, str]:
        if self._fail_stream == "append":
            raise RuntimeError("스트리밍 거부")
        self.appended.append(kwargs)
        return {"ts": kwargs["ts"]}

    def chat_stopStream(self, **kwargs: Any) -> dict[str, str]:
        self.stopped.append(kwargs)
        return {"ts": kwargs["ts"]}

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


def _streaming(client: FakeClient, channel: str = "C1") -> FallbackProgressSink:
    return FallbackProgressSink(
        lambda: SlackStreamingProgressSink(client, channel, "1.0", "이카리 신지"),
        lambda: SlackProgressSink(client, channel, "1.0", "이카리 신지"),
    )


class TestSlackStreamingProgressSink:
    def test_시작한_스레드에_새_줄만_덧붙인다(self) -> None:
        client = FakeClient()
        sink = SlackStreamingProgressSink(client, "C1", "1.0", "이카리 신지")
        sink.open(START_TEXT)
        sink.append(["파일 읽는 중"])
        sink.append(["명령 실행 중"])
        assert client.started[0]["thread_ts"] == "1.0"
        assert client.started[0]["markdown_text"] == START_TEXT
        # 고쳐 쓰기와 달리 이미 보낸 줄을 다시 보내지 않는다
        assert [call["markdown_text"] for call in client.appended] == [
            "\n파일 읽는 중", "\n명령 실행 중",
        ]

    def test_DM_은_스트리밍하지_않는다(self) -> None:
        # chat.startStream 은 thread_ts 가 필수인데 DM 에는 답을 달 스레드가 없다
        with pytest.raises(ProgressStreamUnavailable):
            SlackStreamingProgressSink(FakeClient(), "D1", "1.0")

    def test_끝나면_스트림을_닫고_표시를_지운다(self) -> None:
        client = FakeClient()
        sink = SlackStreamingProgressSink(client, "C1", "1.0")
        sink.open(START_TEXT)
        sink.close()
        assert client.stopped == [{"channel": "C1", "ts": "333.444"}]
        assert client.deleted == [{"channel": "C1", "ts": "333.444"}]

    def test_열지_못했으면_덧붙이지도_닫지도_않는다(self) -> None:
        client = FakeClient()
        sink = SlackStreamingProgressSink(client, "C1", "1.0")
        sink.append([IDLE_TEXT])
        sink.close()
        assert client.appended == [] and client.stopped == [] and client.deleted == []


class TestFallbackProgressSink:
    def test_스트리밍이_되면_고쳐_쓰기를_쓰지_않는다(self) -> None:
        client = FakeClient()
        sink = _streaming(client)
        sink.open(START_TEXT)
        sink.append(["파일 읽는 중"])
        assert len(client.started) == 1
        assert client.posted == [] and client.updated == []

    def test_시작이_거부되면_고쳐_쓰기로_연다(self) -> None:
        client = FakeClient(fail_stream="start")
        sink = _streaming(client)
        sink.open(START_TEXT)
        sink.append(["파일 읽는 중"])
        assert client.started == []
        assert client.posted[0]["text"] == START_TEXT
        assert client.updated[-1]["text"] == f"{START_TEXT}\n파일 읽는 중"

    def test_도중에_거부되면_지금까지_보던_줄을_그대로_다시_낸다(self) -> None:
        client = FakeClient(fail_stream="append")
        sink = _streaming(client)
        sink.open(START_TEXT)
        sink.append(["파일 읽는 중"])
        # 실패한 스트림은 닫아 치우고, 고쳐 쓰기가 누적분으로 다시 연다
        assert client.stopped == [{"channel": "C1", "ts": "333.444"}]
        assert client.posted[0]["text"] == f"{START_TEXT}\n파일 읽는 중"

    def test_전환한_뒤에는_스트리밍을_다시_시도하지_않는다(self) -> None:
        client = FakeClient(fail_stream="append")
        sink = _streaming(client)
        sink.open(START_TEXT)
        sink.append(["파일 읽는 중"])
        sink.append(["명령 실행 중"])
        # 매 틱마다 거부될 요청을 한 번씩 더 보내면 안 된다
        assert client.appended == []
        assert client.updated[-1]["text"] == f"{START_TEXT}\n파일 읽는 중\n명령 실행 중"

    def test_DM_은_처음부터_고쳐_쓰기로_연다(self) -> None:
        client = FakeClient()
        sink = _streaming(client, channel="D1")
        sink.open(START_TEXT)
        assert client.started == []
        assert client.posted[0]["text"] == START_TEXT

    def test_끝나면_쓰고_있던_표시를_닫는다(self) -> None:
        client = FakeClient()
        sink = _streaming(client)
        sink.open(START_TEXT)
        sink.close()
        assert client.deleted == [{"channel": "C1", "ts": "333.444"}]
