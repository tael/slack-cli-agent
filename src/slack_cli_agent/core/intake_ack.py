"""N4: posts the planner's ack once if the answer is still unfinished after T seconds.

The trigger is observed elapsed time, not the planner's predicted type. The
post and the stop flag share one lock, so an ack can never land after the
final answer. Failures never reach the request; they end up in the outcome.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

log = logging.getLogger(__name__)

ACK_LIMIT = 300
STALE_SLACK_SEC = 1.0
#: guard/plan_start's optional ``work`` field. This value means the request has
#: no lookup, write or send in it (an acknowledgement, an agreement, a
#: greeting), so the ack is skipped and the turn posts one answer only.
NO_WORK = "none"


def ack_record_path(plan_file: Path) -> Path:
    """`<transcript>.plan.json` -> `<transcript>.plan.ack.json`."""
    return plan_file.with_name(plan_file.name.removesuffix(".json") + ".ack.json")


@dataclass(frozen=True)
class AckOutcome:
    posted: bool
    posted_at: float | None = None
    latency_sec: float | None = None
    reason: str = ""

    def audit_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {"ack_posted": self.posted}
        if self.posted:
            fields["ack_latency_sec"] = self.latency_sec
        else:
            fields["ack_skip_reason"] = self.reason
        return fields

    def record(self) -> dict[str, Any]:
        return {
            "posted": self.posted, "posted_at": self.posted_at,
            "latency_sec": self.latency_sec, "reason_if_not": self.reason,
        }


def read_plan(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def judge_plan(plan: dict[str, Any] | None, *, start: float) -> tuple[str, str]:
    """(ack text, "") when postable, else ("", reason)."""
    if plan is None:
        return "", "no_plan_file"
    stamp = plan.get("finished_at") or plan.get("started_at")
    if isinstance(stamp, (int, float)) and stamp < start - STALE_SLACK_SEC:
        return "", "stale"
    status = str(plan.get("status") or "unknown")
    if status == "pending":
        return "", "plan_pending"
    if status != "ok":
        return "", f"plan_not_ok:{status}"
    if str(plan.get("work") or "").strip() == NO_WORK:
        return "", "no_work"
    ack = plan.get("ack")
    if not isinstance(ack, str) or not ack.strip():
        return "", "no_ack"
    return ack.strip()[:ACK_LIMIT], ""


def _ts_of(result: object) -> tuple[str, ...]:
    """The posted message ts from whatever ``post`` returned (receipt, ts, list)."""
    if result is None:
        return ()
    message_ts = getattr(result, "message_ts", None)
    if message_ts is not None:
        return tuple(str(t) for t in message_ts if t)
    if isinstance(result, str):
        return (result,) if result else ()
    if isinstance(result, (list, tuple)):
        return tuple(str(t) for t in result if t)
    return ()


class IntakeAck:
    """Context manager entered beside the engine call; one per request."""

    def __init__(
        self,
        *,
        resolve_plan: Callable[[], Path | None],
        post: Callable[[str], object],
        after_sec: float,
        start: float,
        now: Callable[[], float] = time.time,
        poll_sec: float = 1.0,
        alive: Callable[[], bool | None] | None = None,
    ) -> None:
        self._resolve_plan = resolve_plan
        # 2026-11-25 private-taeil: the asker deleted the question mid-turn.
        # The final answer was withheld (origin_deleted) but the ack had
        # already gone up, and with its parent gone it stood alone in the
        # channel. ``alive`` is checked right before the ack posts; only a
        # definite deletion (False) skips, an unknown (None) still posts.
        self._alive = alive
        #: ts of each message the ack put up, so a later origin_deleted can
        #: take it back (see ``retract``).
        self.posted_ts: tuple[str, ...] = ()
        self._post = post
        self._after = after_sec
        self._start = start
        self._now = now
        self._poll = poll_sec
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._posted: AckOutcome | None = None
        self.outcome = AckOutcome(posted=False, reason="not_finished")

    def __enter__(self) -> IntakeAck:
        self._thread = threading.Thread(target=self._run, name="intake-ack", daemon=True)
        self._thread.start()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None,
    ) -> None:
        with self._lock:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        try:
            self._finish()
        except Exception:  # the ack record must not fail the request
            log.warning("ack 기록 실패", exc_info=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            remaining = self._start + self._after - self._now()
            if remaining > 0:
                self._stop.wait(min(remaining, self._poll))
                continue
            text, _ = judge_plan(read_plan(self._safe_resolve()), start=self._start)
            if text and self._try_post(text):
                return
            self._stop.wait(self._poll)

    def _safe_resolve(self) -> Path | None:
        try:
            return self._resolve_plan()
        except Exception:  # noqa: BLE001 -- path lookup is best effort
            return None

    def _try_post(self, text: str) -> bool:
        with self._lock:
            if self._stop.is_set():
                return False
            if self._origin_gone():
                self._posted = AckOutcome(posted=False, reason="origin_deleted")
                return True
            posted_at = self._now()
            try:
                result = self._post(text)
            except Exception as exc:  # a failed ack must not fail the request
                log.warning("ack 게시 실패", exc_info=True)
                self._posted = AckOutcome(posted=False, reason=f"post_failed:{type(exc).__name__}")
                return True
            self.posted_ts = _ts_of(result)
            self._posted = AckOutcome(
                posted=True, posted_at=posted_at, latency_sec=round(posted_at - self._start, 3),
            )
            return True

    def _origin_gone(self) -> bool:
        if self._alive is None:
            return False
        try:
            return self._alive() is False
        except Exception:  # noqa: BLE001 -- an unknown stays unknown, the ack still posts
            log.warning("ack 전 물음 생존 확인 실패", exc_info=True)
            return False

    def retract(self, delete: Callable[[str], object]) -> int:
        """Deletes the ack messages already posted; returns how many went.

        Called when the question turned out deleted at answer time: the
        answer is withheld, and an ack left behind is a reply with no
        question (2026-11-25). A failed delete is logged, never raised.
        """
        with self._lock:
            self._stop.set()
            targets, self.posted_ts = self.posted_ts, ()
        gone = 0
        for ts in targets:
            try:
                delete(ts)
                gone += 1
            except Exception:  # noqa: BLE001 -- cleanup must not fail the request
                log.warning("ack 회수 실패 ts=%s", ts, exc_info=True)
        return gone

    def _finish(self) -> None:
        path = self._safe_resolve()
        if self._posted is not None:
            self.outcome = self._posted
        else:
            _, reason = judge_plan(read_plan(path), start=self._start)
            if not reason:
                reason = (
                    "answer_before_threshold" if self._now() - self._start < self._after
                    else "answer_before_post"
                )
            self.outcome = AckOutcome(posted=False, reason=reason)
        if path is not None:
            target = ack_record_path(path)
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_text(json.dumps(self.outcome.record(), ensure_ascii=False), encoding="utf-8")
            tmp.replace(target)
