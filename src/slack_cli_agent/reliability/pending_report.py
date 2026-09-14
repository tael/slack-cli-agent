"""Persists a report that failed to send during a self-restart, for retry.

`SelfRestarter._announce` can fail to notify right when the socket is
unstable, which would otherwise mean an operator never learns a restart
happened. This is a single slot, not a queue — only the most recent
unsent report is kept.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
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
            self._path.write_text(
                json.dumps({"at": self._now(), "text": text}, ensure_ascii=False)
            )
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
            try:
                claimed.unlink()
            except OSError:
                pass
            return
        try:
            claimed.rename(self._path)
        except OSError as exc:
            log.error("보고를 되돌리지 못했다 : %s", exc)
