"""진행 표시 계층 시험 — 도구 이름 -> 단계 문구, 로그 새 줄 읽기, 유휴 판단."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from slack_cli_agent.config.channel import ChannelConfig
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.observability.progress import (
    IDLE_TEXT,
    START_TEXT,
    ProgressLogReader,
    ProgressTracker,
    ToolLabelMapper,
    channel_progress_enabled,
)


class TestToolLabelMapper:
    @pytest.fixture
    def mapper(self) -> ToolLabelMapper:
        return ToolLabelMapper()

    def test_등록된_접두어는_해당_문구를_돌려준다(self, mapper: ToolLabelMapper) -> None:
        assert mapper.label_for("Read") == "파일 읽는 중"
        assert mapper.label_for("Bash") == "명령 실행 중"

    def test_mcp_도구는_서버_이름으로_문구를_만든다(self, mapper: ToolLabelMapper) -> None:
        # 표에 없는 신규 mcp 서버라도 이름이 그대로 노출되지 않고 일반화된다
        assert mapper.label_for("mcp__새서버__무엇") == "새서버 조회 중"

    def test_표에_있는_mcp_서버는_전용_문구를_쓴다(self, mapper: ToolLabelMapper) -> None:
        assert mapper.label_for("mcp__github__search_code") == "깃헙 코드 보는 중"

    def test_서버_이름을_못_가르면_조회_중으로_돌려준다(self, mapper: ToolLabelMapper) -> None:
        assert mapper.label_for("mcp__") == "조회 중"

    def test_모르는_도구는_기본_문구다(self, mapper: ToolLabelMapper) -> None:
        assert mapper.label_for("무엇인지모를도구") == "확인하는 중"

    def test_빈_이름도_기본_문구다(self, mapper: ToolLabelMapper) -> None:
        assert mapper.label_for("") == "확인하는 중"


class TestProgressLogReader:
    def _write_lines(self, path: Path, tools: list[str]) -> None:
        path.write_text(
            "".join(json.dumps({"tool": t}, ensure_ascii=False) + "\n" for t in tools),
            encoding="utf-8",
        )

    def test_파일이_없으면_빈_목록이다(self, tmp_path: Path) -> None:
        reader = ProgressLogReader()
        assert reader.read_new_labels(tmp_path / "없음.log") == []

    def test_새_줄만큼만_단계_문구로_바뀐다(self, tmp_path: Path) -> None:
        log_path = tmp_path / "s1.log"
        self._write_lines(log_path, ["Read", "Bash"])
        reader = ProgressLogReader()
        assert reader.read_new_labels(log_path) == ["파일 읽는 중", "명령 실행 중"]
        # 다시 읽으면 이미 읽은 자리 뒤로는 아무것도 없다
        assert reader.read_new_labels(log_path) == []

    def test_연달아_같은_단계는_하나로_줄인다(self, tmp_path: Path) -> None:
        log_path = tmp_path / "s1.log"
        self._write_lines(log_path, ["Read", "Read", "Bash", "Bash", "Read"])
        reader = ProgressLogReader()
        assert reader.read_new_labels(log_path) == ["파일 읽는 중", "명령 실행 중", "파일 읽는 중"]

    def test_이전_틱의_마지막_단계와_같아도_줄인다(self, tmp_path: Path) -> None:
        log_path = tmp_path / "s1.log"
        self._write_lines(log_path, ["Read"])
        reader = ProgressLogReader()
        assert reader.read_new_labels(log_path) == ["파일 읽는 중"]
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"tool": "Read"}) + "\n")
        assert reader.read_new_labels(log_path) == []

    def test_잘린_마지막_줄은_다음_틱으로_미룬다(self, tmp_path: Path) -> None:
        log_path = tmp_path / "s1.log"
        log_path.write_text(json.dumps({"tool": "Read"}) + "\n", encoding="utf-8")
        reader = ProgressLogReader()
        assert reader.read_new_labels(log_path) == ["파일 읽는 중"]
        with open(log_path, "a", encoding="utf-8") as f:
            f.write('{"tool": "Ba')  # 쓰는 도중
        assert reader.read_new_labels(log_path) == []
        with open(log_path, "a", encoding="utf-8") as f:
            f.write('sh"}\n')
        assert reader.read_new_labels(log_path) == ["명령 실행 중"]

    def test_reset하면_오프셋과_마지막_단계가_초기화된다(self, tmp_path: Path) -> None:
        log_path = tmp_path / "s1.log"
        self._write_lines(log_path, ["Read"])
        reader = ProgressLogReader()
        reader.read_new_labels(log_path)
        reader.reset()
        assert reader.read_new_labels(log_path) == ["파일 읽는 중"]

    def test_깨진_줄은_건너뛴다(self, tmp_path: Path) -> None:
        log_path = tmp_path / "s1.log"
        log_path.write_text("이건 json이 아니다\n" + json.dumps({"tool": "Read"}) + "\n",
                             encoding="utf-8")
        reader = ProgressLogReader()
        assert reader.read_new_labels(log_path) == ["파일 읽는 중"]


class TestProgressTracker:
    @pytest.fixture
    def clock(self) -> dict:
        return {"now": 1_700_000_000.0}

    @pytest.fixture
    def tracker(self, clock: dict) -> ProgressTracker:
        settings = RuntimeSettings(progress_idle_sec=45)
        return ProgressTracker(settings, now=lambda: clock["now"])

    def test_start는_시작_문구를_돌려준다(self, tracker: ProgressTracker) -> None:
        assert tracker.start() == START_TEXT

    def test_로그_경로가_없으면_새_단계가_없다(self, tracker: ProgressTracker) -> None:
        tracker.start()
        assert tracker.poll(None) == []

    def test_새_단계가_있으면_그대로_돌려준다(
        self, tracker: ProgressTracker, tmp_path: Path
    ) -> None:
        tracker.start()
        log_path = tmp_path / "s1.log"
        log_path.write_text(json.dumps({"tool": "Bash"}) + "\n", encoding="utf-8")
        assert tracker.poll(log_path) == ["명령 실행 중"]

    def test_유휴_시간을_넘기지_않으면_아무것도_안_낸다(
        self, tracker: ProgressTracker, clock: dict, tmp_path: Path
    ) -> None:
        tracker.start()
        clock["now"] += 10
        assert tracker.poll(tmp_path / "없음.log") == []

    def test_유휴_시간을_넘기면_한_줄만_낸다(
        self, tracker: ProgressTracker, clock: dict, tmp_path: Path
    ) -> None:
        tracker.start()
        clock["now"] += 46
        assert tracker.poll(tmp_path / "없음.log") == [IDLE_TEXT]
        # 방금 냈으니 다시 유휴 기준시각이 갱신되어 곧바로 또 나오지 않는다
        clock["now"] += 1
        assert tracker.poll(tmp_path / "없음.log") == []

    def test_start_전에는_poll해도_아무것도_안_낸다(self, tracker: ProgressTracker) -> None:
        assert tracker.poll(None) == []

    def test_새_요청_시작하면_이전_로그_읽기_위치가_초기화된다(
        self, tracker: ProgressTracker, tmp_path: Path
    ) -> None:
        log_path = tmp_path / "s1.log"
        log_path.write_text(json.dumps({"tool": "Read"}) + "\n", encoding="utf-8")
        tracker.start()
        assert tracker.poll(log_path) == ["파일 읽는 중"]
        tracker.start()
        assert tracker.poll(log_path) == ["파일 읽는 중"]


class TestChannelProgressEnabled:
    def test_설정이_없으면_켜짐이다(self) -> None:
        assert channel_progress_enabled(None) is True

    def test_progress_키가_없으면_켜짐이다(self) -> None:
        config = ChannelConfig.from_dict("C1", {})
        assert channel_progress_enabled(config) is True

    def test_progress_참이면_켜짐이다(self) -> None:
        config = ChannelConfig.from_dict("C1", {"progress": True})
        assert channel_progress_enabled(config) is True

    def test_progress_거짓이면_꺼짐이다(self) -> None:
        config = ChannelConfig.from_dict("C1", {"progress": False})
        assert channel_progress_enabled(config) is False


class Test진행_표시는_기본으로_켠다:
    """rich 와 같은 구조였다 - 옵트인이라 sca-tc1 의 task_card 가 거의 안
    떴다. 긴 작업이 도는 동안 아무 표시도 안 나오는 것이 기본값이면 안 된다
    (sca-stj)."""

    def test_아무것도_안_적으면_켜진다(self) -> None:
        from slack_cli_agent.config.channel import ChannelConfig

        assert ChannelConfig(channel_id="C1").progress is True

    def test_등록_안_된_채널도_켜진다(self) -> None:
        from slack_cli_agent.observability.progress import channel_progress_enabled

        assert channel_progress_enabled(None) is True

    def test_끄려면_명시해야_한다(self) -> None:
        from slack_cli_agent.config.channel import ChannelConfig
        from slack_cli_agent.observability.progress import channel_progress_enabled

        assert channel_progress_enabled(ChannelConfig(channel_id="C1", progress=False)) is False
