"""Shared control flow for post-answer review tasks (postmortem, debug trace, format review).

Subclasses only implement `build_prompt()` / `build_header()`; this base class
handles dedup, ledger bookkeeping, running the engine, and splitting the
response into a summary (posted to the channel) and detail (posted to the
thread).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Protocol, runtime_checkable

from slack_cli_agent.engine.base import EngineResponse
from slack_cli_agent.observability.audit import REVIEW_KIND
from slack_cli_agent.review.ledger import ReviewLedger

log = logging.getLogger(__name__)

# Shared by all review kinds so the split marker can't drift out of sync between them.
REVIEW_SPLIT = "===상세==="


def cell(value: Any) -> str:
    # A stray pipe or newline would break the table row it's in and everything after it.
    return str(value).replace("|", "/").replace("\n", " ").strip() or "-"


def as_table(
    rows: Sequence[tuple[Any, ...]], head: tuple[str, ...] = ("항목", "값")
) -> str:
    if not rows:
        return ""
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for row in rows:
        lines.append("| " + " | ".join(cell(c) for c in row) + " |")
    return "\n".join(lines)


def model_effort_cell(record: Mapping[str, Any] | None) -> str:
    record = record or {}
    asked = record.get("model") or "?"
    actual = record.get("model_actual")
    shown = asked if not actual or actual == asked else f"{asked} (실제 {actual})"
    return f"{shown} / {record.get('effort') or '?'}"


@dataclass(frozen=True)
class ReviewTarget:
    channel: str
    ts: str
    by_user: str
    # Resolved by the caller via load_channels(); channel registry isn't this package's concern.
    channel_name: str
    # Resolved by the caller via is_rich(); whether the channel renders markdown blocks or plain text.
    rich: bool


# All ports below are runtime_checkable so wiring code can assert conformance in tests;
# without it, isinstance() raises TypeError instead of a clean check.
@runtime_checkable
class MessageLookupPort(Protocol):
    def find(self, channel: str, ts: str) -> Mapping[str, Any] | None: ...


@runtime_checkable
class TranscriptPort(Protocol):
    def transcript(self, channel: str, thread_ts: str) -> str: ...


@runtime_checkable
class AnswerRecordFinderPort(Protocol):
    def find(self, channel: str, thread_ts: str, text: str) -> Mapping[str, Any] | None: ...


@runtime_checkable
class ReactionPort(Protocol):
    def mark_processing(self, channel: str, ts: str) -> None: ...
    def clear_processing(self, channel: str, ts: str) -> None: ...


@runtime_checkable
class PermalinkPort(Protocol):
    """Returns "" on failure."""

    def permalink(self, channel: str, ts: str) -> str: ...


@runtime_checkable
class PublisherPort(Protocol):
    """Returns None on failure."""

    def post(self, channel: str, thread_ts: str | None, text: str, *, rich: bool) -> str | None: ...


@runtime_checkable
class EngineCaller(Protocol):
    # Assembling the EngineRequest (workdir, model, effort, system prompt) belongs to the
    # wiring layer that knows about Profile/RuntimeSettings; this package only passes the
    # prompt and session id through.
    def run(
        self, prompt: str, session_id: str | None, resume: bool, progress_log: Path | None = None
    ) -> EngineResponse: ...


@runtime_checkable
class ReviewAuditPort(Protocol):
    """AuditLog.record's shape. Reviews spend minutes per engine call and
    none of it was recorded, so there was no way to tell what timeout the
    engine actually needs (sca-fy5)."""

    def record(self, kind: str, *, channel: str = "", thread_ts: str = "", **fields: Any) -> None: ...


@runtime_checkable
class ReviewProgressPort(Protocol):
    """Shows that the review is running, for the length of the engine call.

    A review takes minutes -- measured at 4 for claude and past the 900s
    limit for gemini (sca-tfd). Without this the only signal is the eyes
    reaction, so a review that is working looks the same as one that is not.

    `display` yields the log path the engine's tool hook should write to;
    the display reads that same file. Yielding None means no display for
    this target, and the engine then runs without a hook.

    Cosmetic by contract: the review must finish even when this fails.
    """

    def display(
        self, target: ReviewTarget, thread_ts: str
    ) -> AbstractContextManager[Path | None]: ...


class ReviewTask(ABC):
    # Value used for `reviews.kind`; set by each subclass.
    log_name: ClassVar[str]
    # Reaction emoji that triggers this review. Dispatch happens outside this package;
    # this is display-only here.
    emoji: ClassVar[str]

    def __init__(
        self,
        *,
        ledger: ReviewLedger,
        message_lookup: MessageLookupPort,
        transcript: TranscriptPort,
        answer_finder: AnswerRecordFinderPort,
        reactions: ReactionPort,
        permalinks: PermalinkPort,
        publisher: PublisherPort,
        engine: EngineCaller,
        troubleshoot_channel: str,
        progress: ReviewProgressPort | None = None,
        audit: ReviewAuditPort | None = None,
    ) -> None:
        self._ledger = ledger
        self._message_lookup = message_lookup
        self._transcript = transcript
        self._answer_finder = answer_finder
        self._reactions = reactions
        self._permalinks = permalinks
        self._publisher = publisher
        self._engine = engine
        self._troubleshoot_channel = troubleshoot_channel
        self._progress = progress
        self._audit = audit

    @abstractmethod
    def build_prompt(self, target: ReviewTarget, *, transcript: str, flagged: str, question: str) -> str: ...

    @abstractmethod
    def build_header(self, target: ReviewTarget, record: Mapping[str, Any] | None, link: str) -> str: ...

    def split_marker(self) -> str:
        return REVIEW_SPLIT

    def retry_on_missing_split(self) -> bool:
        # Subclasses that need the split enforced (e.g. strict summary/detail format)
        # override this to retry; the rest just warn and post as-is.
        return False

    def missing_split_prompt(self) -> str:
        """Retry request for output that came back without the split marker.

        Only subclasses with `retry_on_missing_split()` True need to implement this.
        Doesn't take the previous output as an argument: the retry resumes the same
        session, so the model already has it, and if resume fails the retry itself
        fails rather than silently dropping the result.
        """
        raise NotImplementedError

    # Each review kind has its own stopped/failure wording (retry hints reference a
    # different emoji per kind).
    def stopped_title(self) -> str:
        return f"{self.log_name} 중단"

    def not_ok_message(self, body: str) -> str:
        return f"점검을 마치지 못했습니다. {body}"

    def retry_hint(self) -> str:
        return "다시 리액션을 붙이면 재시도합니다."

    def run(self, target: ReviewTarget) -> None:
        if self._ledger.is_reviewed(self.log_name, target.channel, target.ts):
            return
        self._ledger.begin(self.log_name, target.channel, target.ts, by=target.by_user)
        try:
            self._execute(target)
        except Exception as exc:  # noqa: BLE001 — any failure must roll back the ledger and notify
            self._ledger.drop(self.log_name, target.channel, target.ts)
            self._reactions.clear_processing(target.channel, target.ts)
            self._publisher.post(
                self._troubleshoot_channel,
                None,
                f"*{self.stopped_title()}*\n대상 : {target.channel}:{target.ts}\n"
                f"사유 : {exc}\n\n{self.retry_hint()}",
                rich=True,
            )

    @contextmanager
    def _progress_display(self, target: ReviewTarget, thread_ts: str) -> Iterator[Path | None]:
        """Falls back to no display rather than failing the review.

        Only the entry is guarded: a display that breaks after it opened
        leaves its own line behind, which is a cosmetic problem, while
        stopping the review here would lose work already paid for.
        """
        if self._progress is None:
            yield None
            return
        try:
            display: AbstractContextManager[Path | None] = self._progress.display(
                target, thread_ts
            )
            entered = display.__enter__()
        except Exception as exc:  # noqa: BLE001 - see ReviewProgressPort
            log.debug("진행 표시를 열지 못했다 : %s", exc)
            with nullcontext(None) as 없음:
                yield 없음
            return
        try:
            yield entered
        except BaseException as exc:
            if not display.__exit__(type(exc), exc, exc.__traceback__):
                raise
        else:
            display.__exit__(None, None, None)

    def _record_run(self, target: ReviewTarget, response: EngineResponse, *, attempt: str) -> None:
        """Best effort by contract: a review that already ran must not be
        lost because its record could not be written."""
        if self._audit is None:
            return
        try:
            self._audit.record(
                REVIEW_KIND,
                channel=target.channel,
                target_ts=target.ts,
                review_kind=self.log_name,
                attempt=attempt,
                engine=response.engine,
                model=response.model_actual or "",
                elapsed=response.elapsed,
                ok=response.ok,
                turns=response.turns,
                failure=response.failure_reason or "",
            )
        except Exception as exc:  # noqa: BLE001 - see docstring
            log.warning("점검 실행 기록을 남기지 못했다 : %s", exc)

    def _execute(self, target: ReviewTarget) -> None:
        msg = self._message_lookup.find(target.channel, target.ts)
        if msg is None:
            self._ledger.drop(self.log_name, target.channel, target.ts)
            return

        self._reactions.mark_processing(target.channel, target.ts)

        thread_ts = msg.get("thread_ts") or target.ts
        flagged = (msg.get("text") or "").strip()
        transcript = self._transcript.transcript(target.channel, thread_ts)
        record = self._answer_finder.find(target.channel, thread_ts, flagged)
        question = (record or {}).get("question") or ""

        prompt = self.build_prompt(target, transcript=transcript, flagged=flagged, question=question)
        # None: the engine that runs this mints the ID in its own format.
        # Minting a UUID here happened to work only because all three CLIs
        # accept one today (sca-k6s).
        with self._progress_display(target, thread_ts) as progress_log:
            response = self._engine.run(prompt, None, False, progress_log)
        self._record_run(target, response, attempt="main")

        link = self._permalinks.permalink(target.channel, target.ts)
        header = self.build_header(target, record, link)
        self._reactions.clear_processing(target.channel, target.ts)

        if not response.ok:
            self._publisher.post(
                self._troubleshoot_channel, None,
                header + self.not_ok_message(response.body) + f"\n\n{self.retry_hint()}",
                rich=True,
            )
            self._ledger.drop(self.log_name, target.channel, target.ts)
            return

        body = response.body
        marker = self.split_marker()
        if marker not in body and self.retry_on_missing_split() and response.session_id:
            # Resume the session the engine actually used. Without an ID there
            # is nothing to continue, so the response goes out as it came.
            retry = self._engine.run(self.missing_split_prompt(), response.session_id, True)
            self._record_run(target, retry, attempt="split_retry")
            if retry.ok and marker in retry.body:
                body = retry.body

        if marker in body:
            summary, detail = body.split(marker, 1)
        else:
            summary, detail = body, ""

        parent = self._publisher.post(self._troubleshoot_channel, None, header + summary.strip(), rich=True)
        if detail.strip():
            if parent:
                self._publisher.post(self._troubleshoot_channel, parent, detail.strip(), rich=True)
            else:
                self._publisher.post(self._troubleshoot_channel, None, detail.strip(), rich=True)

        report_link = ""
        if parent:
            report_link = self._permalinks.permalink(self._troubleshoot_channel, parent)

        self._ledger.complete(
            self.log_name, target.channel, target.ts,
            by=target.by_user, link=link, report=report_link,
        )
