"""The owner's daily DM anomaly report (E8, mametchi bot.py:7525
dm_daily_report and 7567 dm_daily_loop).

Once a day at or after `run_hour` KST, yesterday's dm_ audit rows go to a
tool-less turn, and its reading goes to the owner's DM. No rows sends a
"기록 없음" line; a failed turn sends the failure with the row count, so a
silent day and a broken report never look the same.

The day is marked done right before the turn runs, as in mametchi: a turn
that hangs or fails is not retried every tick. A tick that gets no engine
slot marks nothing and tries again on the next one.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..core.timezones import KST
from ..observability.audit import IncidentKind
from ..observability.notices import NoticeCatalog, NoticeKey
from .service import TurnResult

log = logging.getLogger(__name__)

#: Fields sent to the analysis, as in mametchi. The rest (channel ids,
#: epoch times) only costs tokens.
REPORT_FIELDS: tuple[str, ...] = (
    "kind", "user", "dm_kind", "fallback", "probe", "question", "reply",
    "restriction", "error", "count_today", "ts_kst",
)
MAX_ROWS = 200
MAX_INPUT_CHARS = 60000
MAX_FIELD_CHARS = 500


class DmDailyReport:
    def __init__(
        self,
        *,
        # (start, end, prefix) -> audit rows, AuditLog.rows_between.
        rows: Callable[[float, float, str], Sequence[Mapping[str, Any]]],
        run_turn: Callable[[str], TurnResult],
        notify_owner: Callable[[str], object],
        audit: Callable[..., None],
        notices: NoticeCatalog,
        state_path: Path,
        run_hour: int = 9,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not 0 <= run_hour <= 23:
            raise ValueError(f"보고 시각은 0-23 이어야 한다 : {run_hour}")
        self._rows = rows
        self._run_turn = run_turn
        self._notify_owner = notify_owner
        self._audit = audit
        self._notices = notices
        self._state_path = state_path
        self._run_hour = run_hour
        self._clock = clock

    def due_day(self) -> str | None:
        """Yesterday's date once today's run hour has passed and today has
        not reported yet; None otherwise."""
        now = datetime.fromtimestamp(self._clock(), KST)
        if now.hour < self._run_hour or self._last() == now.strftime("%Y-%m-%d"):
            return None
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")

    def tick(self, engine: Callable[[], AbstractContextManager[None]]) -> None:
        """One check, run by the background job. `engine` is the slot
        section (G1); SlotUnavailable from it leaves the day unmarked."""
        day = self.due_day()
        if day is None:
            return
        rows = self._day_rows(day)
        if not rows:
            self._mark_done()
            self._send(self._notices.render(NoticeKey.DM_REPORT_EMPTY, day=day))
            self._record(day, 0, ok=True)
            return
        with engine():
            self._mark_done()
            body, reason = self._analyze(rows)
        if body is None:
            self._send(self._notices.render(
                NoticeKey.DM_REPORT_FAILED, day=day, reason=reason, count=len(rows),
            ))
            self._record(day, len(rows), ok=False, reason=reason)
            return
        self._send(self._notices.render(NoticeKey.DM_REPORT, day=day, body=body))
        self._record(day, len(rows), ok=True)

    def _day_rows(self, day: str) -> list[dict[str, Any]]:
        start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=KST).timestamp()
        out: list[dict[str, Any]] = []
        for row in self._rows(start, start + 86400, "dm_"):
            # The report's own rows would make every day look active.
            if row.get("kind") == IncidentKind.DM_REPORT.value:
                continue
            record = dict(row)
            at = record.get("at")
            if isinstance(at, (int, float)):
                record["ts_kst"] = datetime.fromtimestamp(at, KST).isoformat(timespec="seconds")
            out.append({
                key: (value[:MAX_FIELD_CHARS] if isinstance(value, str) else value)
                for key, value in record.items() if key in REPORT_FIELDS
            })
        return out

    def _analyze(self, rows: Sequence[Mapping[str, Any]]) -> tuple[str | None, str]:
        prompt = json.dumps(list(rows[-MAX_ROWS:]), ensure_ascii=False)[:MAX_INPUT_CHARS]
        try:
            result = self._run_turn(prompt)
        except Exception as exc:  # noqa: BLE001 - reported to the owner as a failure
            log.warning("DM 일일 점검 분석을 하지 못했다 : %s", exc)
            return None, type(exc).__name__
        if not result.ok:
            return None, result.failure_reason or "알 수 없음"
        body = "\n".join(line for line in (result.body or "").splitlines() if "Vooster" not in line).strip()
        return (body or None), "빈 답"

    def _last(self) -> str | None:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return str(data.get("last") or "") or None if isinstance(data, dict) else None

    def _mark_done(self) -> None:
        today = datetime.fromtimestamp(self._clock(), KST).strftime("%Y-%m-%d")
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps({"last": today}), encoding="utf-8")
        except OSError as exc:
            log.warning("DM 일일 점검 실행 기록을 남기지 못했다 : %s", exc)

    def _send(self, text: str) -> None:
        try:
            self._notify_owner(text)
        except Exception as exc:  # noqa: BLE001 - the next day still runs
            log.warning("DM 일일 점검을 보내지 못했다 : %s", exc)

    def _record(self, day: str, count: int, **fields: Any) -> None:
        try:
            self._audit(IncidentKind.DM_REPORT, day=day, rows=count, **fields)
        except Exception as exc:  # noqa: BLE001 - the report already went out
            log.warning("DM 일일 점검 감사 기록을 남기지 못했다 : %s", exc)
