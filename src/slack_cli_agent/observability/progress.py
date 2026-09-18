# Shows which tool is currently running rather than an elapsed-seconds
# counter -- the display only appends, so a counter would pile up one line
# per tick and get unreadable on long-running requests.
#
# What to say (ToolLabelMapper/ProgressTracker) and when to say it
# (ProgressSession's polling thread) both live here, so one implementation
# covers every engine and every channel. Only the two ends are pluggable:
# an engine supplies the log file its own tool hook writes, and a
# ProgressSink puts the lines somewhere a person can see. Neither end is
# imported here, so this is testable without an engine or a Slack client.

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from ..config.channel import ChannelConfig
from ..config.settings import RuntimeSettings

log = logging.getLogger(__name__)

START_TEXT = "작업 중"
# Emitted when there's been no new tool call for a while, so a long
# generation-only stretch doesn't look like it stalled.
IDLE_TEXT = "아직 작업 중"
DEFAULT_LABEL = "확인하는 중"

#: Audit kind for a tool name the table has no entry for. Matches
#: IncidentKind.PROGRESS_UNKNOWN_TOOL; kept as a literal so this module
#: does not import the audit layer.
UNKNOWN_TOOL_KIND = "progress_unknown_tool"

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
    # codex — item.type 이 도구 이름 자리다 (2026-09-19 실측).
    ("command_execution", "명령 실행 중"),
    ("file_change", "파일 고치는 중"),
    ("web_search", "웹 찾는 중"),
    ("mcp_tool_call", "조회 중"),
    ("agent_message", "답 쓰는 중"),
    ("agent_response", "답 쓰는 중"),
    # agy — step_update.tool_name. 이름은 init 이벤트의 도구 목록에서 가져왔다.
    # 앞이 긴 이름부터 온다: 첫 접두어가 이기므로 browser_subagent 가
    # browser_ 보다 뒤에 있으면 브라우저 문구로 덮인다.
    ("browser_subagent", "따로 조사 돌리는 중"),
    ("invoke_subagent", "따로 조사 돌리는 중"),
    ("run_command", "명령 실행 중"),
    ("command_status", "명령 실행 중"),
    ("view_file", "파일 읽는 중"),
    ("notebook_edit", "파일 고치는 중"),
    ("grep_search", "코드 찾는 중"),
    ("find_by_name", "파일 찾는 중"),
    ("list_dir", "파일 찾는 중"),
    ("replace_file_content", "파일 고치는 중"),
    ("multi_replace_file_content", "파일 고치는 중"),
    ("sed_file", "파일 고치는 중"),
    ("write_to_file", "파일 쓰는 중"),
    ("search_web", "웹 찾는 중"),
    ("read_url_content", "웹 문서 읽는 중"),
    ("call_mcp_tool", "조회 중"),
    ("browser_", "브라우저로 화면 보는 중"),
    ("open_browser_url", "브라우저로 화면 보는 중"),
    ("read_browser_page", "브라우저로 화면 보는 중"),
    ("capture_browser_", "브라우저로 화면 보는 중"),
    ("click_browser_pixel", "브라우저로 화면 보는 중"),
    ("execute_browser_javascript", "브라우저로 화면 보는 중"),
    ("list_browser_pages", "브라우저로 화면 보는 중"),
    ("manage_task", "할 일 정리 중"),
    ("generate_image", "그림 만드는 중"),
)


class ToolLabelMapper:
    """Maps a tool name to a progress label, falling back to the mcp server name."""

    def __init__(
        self,
        labels: Sequence[tuple[str, str]] = TOOL_LABELS,
        on_unknown: Callable[[str], None] | None = None,
    ) -> None:
        self._labels = tuple(labels)
        # Told about names the table has no entry for, so a CLI renaming its
        # tools is visible instead of silently showing the default wording.
        # Deduped here: progress runs many times per request, and recording
        # every call would bury the ledger (sca-2wu).
        self._on_unknown = on_unknown
        self._reported: set[str] = set()

    def label_for(self, tool_name: str) -> str:
        if not tool_name:
            return DEFAULT_LABEL
        for prefix, label in self._labels:
            if tool_name.startswith(prefix):
                return label
        if tool_name.startswith("mcp__"):
            parts = tool_name.split("__")
            server = parts[1] if len(parts) > 2 else ""
            # The server name carries the meaning, so there is nothing to add
            # to the table -- not reported.
            return f"{server} 조회 중" if server else "조회 중"
        self._report_unknown(tool_name)
        return DEFAULT_LABEL

    def _report_unknown(self, tool_name: str) -> None:
        if self._on_unknown is None or tool_name in self._reported:
            return
        self._reported.add(tool_name)
        try:
            self._on_unknown(tool_name)
        except Exception:  # noqa: BLE001 - progress display must not fail a request
            log.warning("표에 없는 도구 이름 기록 실패 : %s", tool_name)


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
    # None means the channel is not in channels.json, not that it wants a
    # quiet thread -- a bot answers a mention anywhere (sca-stj).
    if config is None:
        return ChannelConfig(channel_id="").progress
    return bool(config.progress)


class AuditPort(Protocol):
    """AuditLog.record's shape. Optional -- progress runs without one."""

    def __call__(self, kind: str, **fields: Any) -> None: ...


def _unknown_tool_reporter(audit: AuditPort | None) -> Callable[[str], None] | None:
    if audit is None:
        return None

    def report(tool_name: str) -> None:
        audit(UNKNOWN_TOOL_KIND, tool=tool_name)

    return report


class ProgressSink(Protocol):
    """Where the step lines go. One instance per request.

    Implementations are cosmetic by contract: raising from any of these
    would take down a request that was otherwise answered fine, so
    ProgressSession swallows whatever they raise. They may still raise —
    the swallowing is here, not duplicated in every implementation.
    """

    def open(self, text: str) -> None:
        """Shows the first line. Called once, before any append()."""

    def append(self, lines: Sequence[str]) -> None:
        """Adds finished step lines. Called zero or more times."""

    def close(self) -> None:
        """Takes the display down. Called exactly once, including on failure."""


class ProgressSession:
    """Polls the log on a timer and pushes new step lines to a sink.

    A thread rather than a callback on the engine call: the engine runs as
    a subprocess and gives the bot process nothing until it exits, so the
    only thing that can report intermediate progress is something watching
    the hook's log file alongside it.
    """

    def __init__(
        self,
        tracker: ProgressTracker,
        sink: ProgressSink,
        log_path: Path,
        tick_sec: float,
    ) -> None:
        self._tracker = tracker
        self._sink = sink
        self._log_path = log_path
        self._tick_sec = tick_sec
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        # Truncate rather than delete: the hook appends, and a resumed
        # session's file still holds the previous request's lines.
        self._write_empty()
        self._emit(lambda: self._sink.open(self._tracker.start()))
        self._thread = threading.Thread(target=self._loop, name="progress", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            # Bounded so a sink call wedged on a network timeout can't hold
            # the answer back indefinitely; the thread is a daemon, so a
            # straggler dies with the process rather than blocking shutdown.
            self._thread.join(timeout=self._tick_sec * 2)
            self._thread = None
        self._emit(self._sink.close)
        try:
            self._log_path.unlink(missing_ok=True)
        except OSError as exc:
            log.debug("진행 로그 삭제 실패 : %s, %s", self._log_path, exc)

    def _loop(self) -> None:
        while not self._stop.wait(self._tick_sec):
            labels = self._poll()
            if labels:
                self._send(labels)

    def _send(self, labels: Sequence[str]) -> None:
        self._emit(lambda: self._sink.append(labels))

    def _poll(self) -> list[str]:
        try:
            return self._tracker.poll(self._log_path)
        except OSError as exc:
            log.debug("진행 로그 읽기 실패 : %s, %s", self._log_path, exc)
            return []

    def _write_empty(self) -> None:
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_path.write_text("", encoding="utf-8")
        except OSError as exc:
            log.debug("진행 로그 초기화 실패 : %s, %s", self._log_path, exc)

    @staticmethod
    def _emit(action: Callable[[], None]) -> None:
        try:
            action()
        except Exception as exc:  # noqa: BLE001 - see ProgressSink: the display must not fail the request
            log.debug("진행 표시 실패 : %s", exc)


#: Builds the sink for one request, given its channel, thread and the user
#: who asked. The user is what Slack's streaming API takes as the recipient.
SinkFactory = Callable[[str, str, str], ProgressSink]


class ProgressCoordinator:
    """Entry point for callers: decides whether a request gets progress
    display, where its log goes, and runs the session around the engine call.

    The pipeline holds one of these and knows nothing about tool names,
    tick intervals or Slack. Engines know only the log path they are handed.
    """

    def __init__(
        self,
        settings: RuntimeSettings,
        sink_factory: SinkFactory,
        log_dir: Path,
        mapper: ToolLabelMapper | None = None,
        audit: AuditPort | None = None,
    ) -> None:
        self._settings = settings
        self._sink_factory = sink_factory
        self._log_dir = log_dir
        self._mapper = mapper or ToolLabelMapper(on_unknown=_unknown_tool_reporter(audit))

    def log_path_for(self, config: ChannelConfig | None, channel: str, ts: str) -> Path | None:
        """The log this request's engine should write to, or None when the
        channel has progress off. None is what the engine reads as "no hook"."""
        if not channel_progress_enabled(config):
            return None
        # ts carries a dot; channel and ts are Slack-issued IDs, so neither
        # can contain a path separator.
        return self._log_dir / f"{channel}-{ts}.log"

    @contextmanager
    def session(
        self, channel: str, thread_ts: str, user: str, log_path: Path | None,
    ) -> Generator[None]:
        """Runs progress display for the duration of the block.

        A None log_path means the caller decided this request has no
        progress display, so this is a plain pass-through — callers don't
        branch on it themselves.
        """
        if log_path is None:
            yield
            return
        try:
            sink = self._sink_factory(channel, thread_ts, user)
        except Exception as exc:  # noqa: BLE001 - see ProgressSink
            log.debug("진행 표시를 열지 못했다 : %s", exc)
            yield
            return
        session = ProgressSession(
            ProgressTracker(self._settings, self._mapper), sink, log_path,
            self._settings.progress_tick_sec,
        )
        session.start()
        try:
            yield
        finally:
            session.stop()
