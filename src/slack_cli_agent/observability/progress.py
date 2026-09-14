# Shows which tool is currently running rather than an elapsed-seconds
# counter -- `chat.appendStream` only appends, so a counter would pile up
# one line per tick and get unreadable on long-running requests.
#
# This module only decides what to say; the actual Slack streaming calls
# and their polling thread live in the (not yet written) Slack adapter, so
# this can be tested without a Slack client.

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from ..config.channel import ChannelConfig
from ..config.settings import RuntimeSettings

START_TEXT = "확인하고 있어요."
# Emitted when there's been no new tool call for a while, so a long
# generation-only stretch doesn't look like it stalled.
IDLE_TEXT = "아직 보고 있어요"
DEFAULT_LABEL = "확인하는 중"

# Maps tool names to human-readable step labels; first prefix match wins.
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
    """Maps a tool name to a progress label, falling back to the mcp server name."""

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
    """Reads newly appended `{"tool": "..."}` lines from the hook log and maps them to labels."""

    def __init__(self, mapper: ToolLabelMapper | None = None) -> None:
        self._mapper = mapper or ToolLabelMapper()
        self._offset = 0
        self._last_label = ""

    def reset(self) -> None:
        # A resumed session's log file still has lines from prior requests;
        # without resetting, those stale entries would leak into this run's
        # progress display.
        self._offset = 0
        self._last_label = ""

    def read_new_labels(self, log_path: Path) -> list[str]:
        # Only consume complete (newline-terminated) lines, since the writer
        # may be mid-write on the last one. Collapse consecutive repeats so
        # ten calls to the same tool don't spam ten identical lines.
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
    """Decides what to emit each tick: the caller polls at `progress_tick_sec` and sends whatever comes back."""

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
        self._last_post_at = self._now()
        self._reader.reset()
        return START_TEXT

    def poll(self, log_path: Path | None) -> list[str]:
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
    if config is None:
        return False
    return bool(config.progress)
