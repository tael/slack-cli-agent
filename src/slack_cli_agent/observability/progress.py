"""처리 중 경과를 몇 줄로 요약해 슬랙에 흘리는 계층.

원본 `bot.py` 의 `ProgressStream`(2026-09-03 도입)을 재구성했다. 원본은 답을
다 만든 뒤 한 번에 올려서, 조회가 길어지면 몇 분 동안 아무 표시가 없어 멈춘
것으로 보였다. 처음에는 경과 초를 덧붙이는 방식이었으나 `chat.appendStream`
이 이어붙이기만 되어 오래 걸릴수록 숫자 줄이 쌓여 읽기 어려웠다. 그래서 지금
무슨 도구를 쓰는지 보여주는 방식으로 바뀌었다 — 화면에 남는 줄이 실제 단계
수만큼으로 끝나고, 대기가 길 때 무엇 때문에 긴지 드러난다.

이 모듈은 판단만 한다. 실제 슬랙 스트리밍 호출(`chat.startStream`,
`chat.appendStream`, `chat.stopStream` 등)과 그것을 주기로 부르는 스레드는
슬랙 어댑터 쪽(아직 없음)의 몫이다 — 여기서는 시각과 로그 파일만 있으면
결과가 결정되게 만들어, 슬랙 클라이언트 없이 시험한다.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from ..config.channel import ChannelConfig
from ..config.settings import RuntimeSettings

# 스트림을 열 때 첫 줄. 아직 아무 도구도 안 썼을 때의 상태를 알린다.
START_TEXT = "확인하고 있어요."
# 새 도구 호출이 없어도 유휴 시간을 넘기면 아직 도는 중이라고 남기는 한 줄.
# 생성만 오래 하는 구간에는 도구 호출이 없어 표시가 멈춘 것처럼 보이기 때문이다.
IDLE_TEXT = "아직 보고 있어요"
# 어느 표에도 못 걸리고 mcp 서버 이름도 못 가른 도구의 기본 문구.
DEFAULT_LABEL = "확인하는 중"

# 도구 이름을 사람이 읽는 단계 이름으로 바꾼다. 앞의 것부터 맞춰 보고 처음
# 걸리는 것을 쓴다. 원본 TOOL_LABELS 를 그대로 옮겼다 — 여기 담긴 이름은
# mcp 도구의 일반 서버 이름(github, notion 등)이라 조직 고유값이 아니다.
TOOL_LABELS: Sequence[tuple[str, str]] = (
    ("mcp__slack__", "슬랙 대화 찾는 중"),
    ("mcp__snowflake__", "스노우플레이크 조회 중"),
    ("mcp__metabase__", "메타베이스 조회 중"),
    ("mcp__notion__", "노션 문서 보는 중"),
    ("mcp__atlassian__", "지라 조회 중"),
    ("mcp__jira__", "지라 조회 중"),
    ("mcp__github__", "깃헙 코드 보는 중"),
    ("mcp__google-sheets__", "구글 시트 보는 중"),
    ("mcp__google-calendar__", "일정 보는 중"),
    ("mcp__gmail__", "메일 보는 중"),
    ("mcp__browser__", "브라우저로 화면 보는 중"),
    ("mcp__kubernetes__", "쿠버네티스 조회 중"),
    ("mcp__local-rag__", "사내 문서 찾는 중"),
    ("mcp__context7__", "라이브러리 문서 보는 중"),
    ("Read", "파일 읽는 중"),
    ("Grep", "코드 찾는 중"),
    ("Glob", "파일 찾는 중"),
    ("Edit", "파일 고치는 중"),
    ("Write", "파일 쓰는 중"),
    ("NotebookEdit", "파일 고치는 중"),
    ("Bash", "명령 실행 중"),
    ("WebSearch", "웹 찾는 중"),
    ("WebFetch", "웹 문서 읽는 중"),
    ("Task", "따로 조사 돌리는 중"),
    ("Agent", "따로 조사 돌리는 중"),
    ("Skill", "스킬 여는 중"),
    ("TodoWrite", "할 일 정리 중"),
)


class ToolLabelMapper:
    """도구 이름 하나를 진행 표시 문구로 바꾼다.

    표에 없는 mcp 도구는 서버 이름을 그대로 살려 "<서버> 조회 중" 으로 쓴다.
    표를 못 따라간 신규 도구가 이름 그대로 노출되지 않게 하기 위함이다.
    """

    def __init__(self, labels: Sequence[tuple[str, str]] = TOOL_LABELS) -> None:
        self._labels = tuple(labels)

    def label_for(self, tool_name: str) -> str:
        if not tool_name:
            return DEFAULT_LABEL
        for prefix, label in self._labels:
            if tool_name.startswith(prefix):
                return label
        if tool_name.startswith("mcp__"):
            parts = tool_name.split("__")
            server = parts[1] if len(parts) > 2 else ""
            return f"{server} 조회 중" if server else "조회 중"
        return DEFAULT_LABEL


class ProgressLogReader:
    """훅이 도구 이름을 적어 둔 로그 파일에서 새 줄만 읽어 단계 이름으로 바꾼다.

    훅은 도구를 부를 때마다 `{"tool": "이름"}` 한 줄을 이 파일에 추가한다. 이
    클래스는 그 파일의 어디까지 읽었는지를 스스로 들고 있다가, 다음 호출에서
    새로 늘어난 부분만 본다.
    """

    def __init__(self, mapper: ToolLabelMapper | None = None) -> None:
        self._mapper = mapper or ToolLabelMapper()
        self._offset = 0
        self._last_label = ""

    def reset(self) -> None:
        """세션이 바뀌면 오프셋과 마지막 단계를 초기화한다.

        세션을 이어 쓰면 지난 요청이 남긴 줄이 파일에 그대로 있다. 초기화 없이
        읽으면 이번에 하지도 않은 조회가 진행 표시에 흐른다.
        """
        self._offset = 0
        self._last_label = ""

    def read_new_labels(self, log_path: Path) -> list[str]:
        """마지막으로 읽은 자리 뒤의 완결된 줄만 단계 이름으로 바꿔 돌려준다.

        쓰는 도중에 읽으면 마지막 줄이 잘려 있을 수 있어, 완결된 줄(끝에 개행이
        있는 줄)까지만 소비한다. 연달아 같은 단계가 나오면 하나로 줄인다 —
        같은 줄이 열 번 쌓이면 경과 초를 찍던 옛 방식과 다를 바가 없다.
        """
        try:
            text = log_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        raw = text[self._offset :]
        cut = raw.rfind("\n")
        if cut < 0:
            return []
        self._offset += len(raw[: cut + 1].encode("utf-8"))

        out: list[str] = []
        for line in raw[:cut].splitlines():
            if not line.strip():
                continue
            try:
                tool_name = json.loads(line).get("tool") or ""
            except (json.JSONDecodeError, AttributeError):
                continue
            label = self._mapper.label_for(tool_name)
            previous = out[-1] if out else self._last_label
            if label and label != previous:
                out.append(label)
        if out:
            self._last_label = out[-1]
        return out


class ProgressTracker:
    """처리 중 경과로 무엇을 흘려보낼지 판단한다.

    실제 슬랙 호출은 이 클래스의 책임이 아니다. 호출부가
    `RuntimeSettings.progress_tick_sec` 주기로 `poll()` 을 부르고, 돌아온
    문구를 그대로 발신한다. 이 계층이 두 판단을 맡는다 — 새 도구 호출이
    있으면 그 단계 이름을, 없고 유휴 시간을 넘겼으면 한 줄만.
    """

    def __init__(
        self,
        settings: RuntimeSettings,
        mapper: ToolLabelMapper | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._reader = ProgressLogReader(mapper)
        self._now = now
        self._last_post_at: float | None = None

    def start(self) -> str:
        """새 요청을 시작한다. 시작 문구를 돌려주고 로그 읽기 위치를 초기화한다."""
        self._last_post_at = self._now()
        self._reader.reset()
        return START_TEXT

    def poll(self, log_path: Path | None) -> list[str]:
        """이번 틱에 흘려보낼 문구 목록.

        `start()` 를 부르기 전에는 유휴 기준 시각이 없어 항상 빈 목록이다.
        새 단계가 있으면 그것을 우선한다. 없고 유휴 시간(`progress_idle_sec`)을
        넘겼으면 한 줄만 남긴다. 그 안이면 아무것도 돌려주지 않는다.
        """
        if self._last_post_at is None:
            return []
        labels = self._reader.read_new_labels(log_path) if log_path is not None else []
        if labels:
            self._last_post_at = self._now()
            return labels
        if self._now() - self._last_post_at > self._settings.progress_idle_sec:
            self._last_post_at = self._now()
            return [IDLE_TEXT]
        return []


def channel_progress_enabled(config: ChannelConfig | None) -> bool:
    """이 채널에서 진행 표시를 켤지 본다.

    기본은 꺼짐이다. 원본도 먼저 켠 채널에서 동작을 확인한 뒤 넓혔다.
    """
    if config is None:
        return False
    return bool(config.progress)
