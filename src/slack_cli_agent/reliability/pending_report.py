"""Persists a report that failed to send during a self-restart, for retry.

`SelfRestarter._announce` can fail to notify right when the socket is
unstable, which would otherwise mean an operator never learns a restart
happened. This is a single slot, not a queue — only the most recent
unsent report is kept.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


class PendingReportStore:
    """Saves an unsent report to a file, clearing it once resend succeeds."""

    def __init__(
        self,
        *,
        path: Path,
        sender: Callable[[str], None],
        now: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._sender = sender
        self._now = now

    def save(self, text: str) -> None:
        # Overwrites any previous unsent report; this slot tracks only the
        # most recent one, not a history.
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps({"at": self._now(), "text": text}, ensure_ascii=False))
        except OSError as exc:
            log.error("보고를 남기지 못했다 : %s", exc)

    def flush(self) -> None:
        # Renames the file to claim it first; rename() is atomic, so if
        # ingest and worker both call flush, only one sends it.
        claimed = self._path.with_suffix(self._path.suffix + ".sending")
        try:
            self._path.rename(claimed)
        except OSError:
            return  # nothing pending, or another process already claimed it

        try:
            data = json.loads(claimed.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            # Could be a file left half-written by a restart mid-write; no
            # text to retry with, so just log and put it back.
            log.error("남겨둔 보고를 읽지 못했다 : %s", exc)
            self._restore(claimed)
            return

        text = data.get("text", "")
        try:
            self._sender(text)
        except Exception as exc:  # noqa: BLE001 — sender failures range from SDK errors to network issues; retry next time regardless
            log.error("남겨둔 보고를 보내지 못했다 : %s", exc)
            self._restore(claimed)
            return

        try:
            claimed.unlink()
        except OSError as exc:
            log.warning("보낸 보고 파일을 지우지 못했다 : %s", exc)
        else:
            log.info("남겨둔 보고를 보냈다.")

    def _restore(self, claimed: Path) -> None:
        # If a newer report was saved in the meantime, keep that one instead
        # — this is a single slot, so only the most recent report survives.
        if self._path.exists():
            with contextlib.suppress(OSError):
                claimed.unlink()
            return
        try:
            claimed.rename(self._path)
        except OSError as exc:
            log.error("보고를 되돌리지 못했다 : %s", exc)


@dataclass(frozen=True)
class PendingWork:
    description: str
    started_at: float


class PendingWorkMarker:
    """Marks what was being worked on, so a crash mid-turn can be surfaced
    on recovery instead of silently dropping the asker's request.

    A restart reason alone (SelfRestarter's announcement) says what
    happened to the *bot*; it says nothing about what was in flight for
    the *asker*. This records a description before starting work that
    could outlive the process and clears it once that work finishes
    normally, so anything still on record when the bot comes back up was
    truly interrupted.
    """

    def __init__(self, *, path: Path, now: Callable[[], float] = time.time) -> None:
        self._path = path
        self._now = now

    def start(self, description: str) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(
                    {"description": description, "started_at": self._now()},
                    ensure_ascii=False,
                )
            )
        except OSError as exc:
            log.error("진행 중 표식을 남기지 못했다 : %s", exc)

    def clear(self) -> None:
        with contextlib.suppress(OSError):
            self._path.unlink()

    def peek(self) -> PendingWork | None:
        try:
            data = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        description = data.get("description")
        started_at = data.get("started_at")
        if not description or started_at is None:
            return None
        return PendingWork(description=description, started_at=started_at)


def assemble_recovery_report(
    *,
    reason: str,
    now: float,
    pending: PendingWork | None = None,
    network_outage_sec: float | None = None,
) -> str:
    """Builds the text a recovery announcement sends, folding in whatever
    the plain restart reason wouldn't otherwise say: work that was cut off
    mid-turn, and -- when the cause was network-level -- how long that
    outage ran (G59 widens notification scope to DNS/Wi-Fi outages, not
    just Slack-API-level ones).
    """
    lines = [f"- 사유 : {reason}"]
    if network_outage_sec is not None:
        lines.append(f"- 네트워크 장애 : {network_outage_sec / 60:.0f}분")
    if pending is not None:
        age_min = max(0.0, (now - pending.started_at) / 60)
        lines.append(f"- 중단된 작업 : {pending.description} (시작 후 {age_min:.0f}분)")
    return "\n".join(lines)
