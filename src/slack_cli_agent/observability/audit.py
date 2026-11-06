# Audit records go to both the DB (`audit` table) and an `audit.jsonl` file.
# External tools and dashboards read the jsonl, so field names there must
# stay stable; the DB is for querying and aggregation.

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..core.jsonsafe import dump_json
from ..storage.database import Database
from ..storage.repository import SqliteRepository


class IncidentKind(StrEnum):
    """Every kind string written to the `audit` table's `kind` column.

    Single source for these strings — pipeline.py, publisher.py, and
    metrics.py all read from here instead of repeating the literal, so a
    rename can't leave one caller writing a kind nothing reads back.
    """

    REQUEST = "request"
    #: What the request required of the engine and what that engine actually
    #: enforces. Baseline traffic like REQUEST, not an incident.
    CAPABILITY = "capability"
    #: How many bytes one engine call put on the wire, and how the
    #: instructions travelled. Sizes and kinds only, never the text. Baseline
    #: traffic like REQUEST (sca-ygd).
    PAYLOAD = "payload"
    #: One engine call made by a review task. Baseline traffic like REQUEST;
    #: its elapsed value is what timeout decisions are read from.
    REVIEW = "review"
    #: One round of the watch queue touching one job. Baseline traffic:
    #: checks/last_run only hold current state, so without this there is no
    #: record that a watch ever ran (sca-j3d).
    WATCH_CHECKED = "watch_checked"
    #: A watch that reported and left the queue. Baseline traffic.
    WATCH_FINISHED = "watch_finished"
    #: A watch dropped on the age limit without its work ever finishing.
    WATCH_ABANDONED = "watch_abandoned"
    #: A tool name the progress label table has no entry for, so the step
    #: line fell back to the default wording. Recorded once per name per
    #: process -- a CLI version bump renaming its tools shows up here and
    #: nowhere else (sca-2wu).
    PROGRESS_UNKNOWN_TOOL = "progress_unknown_tool"
    SPLIT = "split"
    BLOCKS_RESPLIT = "blocks_resplit"
    SPLIT_BROKEN = "split_broken"
    BLOCKS_REJECTED = "blocks_rejected"
    POST_FAILED = "post_failed"
    #: A ```csv block was extracted for upload but files_upload_v2 failed
    #: (J14, bot.py:6316 snippet_failed). The raw csv is re-posted inline so
    #: the data isn't lost; this records that the file attachment didn't land.
    CSV_UPLOAD_FAILED = "csv_upload_failed"
    LATE_ADDENDUM = "late_addendum"
    WRONG_ADDRESSEE = "wrong_addressee"
    REWRITE_LOSS = "rewrite_loss"
    #: A rich chat.update came back stored as rich_text only, while the body
    #: carried a heading/table/divider that Slack renders as its own block
    #: (J7, mametchi post_rich.py verify + separate_blocks). A 200 from
    #: chat.update doesn't prove the markdown took; this records that it didn't.
    REWRITE_UNCONVERTED = "rewrite_unconverted"
    SILENT = "silent"
    #: A reply arrived without a `[[RECIPIENT:...]]` tag while recipient
    #: enforcement was on, so its body was withheld and only a notice posted
    #: (J11/D3, bot.py:7872 missing_recipient). Records the block; the
    #: snippet field holds the withheld body's head for troubleshooting.
    MISSING_RECIPIENT = "missing_recipient"
    #: An undeclared `[[...]]` marker line dropped before publish
    #: (strip_unknown_tags, bot.py:1561's UNKNOWN_TAG_LINE_RE). Logged
    #: rather than silently eaten so an invented marker surfaces.
    UNKNOWN_TAG = "unknown_tag"
    #: One published reply's watch disposition, as a `verdict` field:
    #: "watch" (a follow-up watch was registered), "nowatch" (the model
    #: ended with `[[NOWATCH]]` to say none was needed), or "missing"
    #: (neither — it left the call ambiguous). Lets the NOWATCH 판정률 be
    #: aggregated (J9, mametchi bot.py NOWATCH 감사). Baseline traffic.
    WATCH_VERDICT = "watch_verdict"
    #: The question message was gone from its thread at publish time, so the
    #: reply was withheld rather than dropped onto whatever thread now holds
    #: those coordinates (C34, bot.py:8170 origin_deleted). `snippet` holds
    #: the withheld body's head for troubleshooting.
    ORIGIN_DELETED = "origin_deleted"
    #: The engine had already posted a file in the thread through a tool, so
    #: the final answer was written over that message instead of posted as a
    #: second reply (2026-11-06 duplicate replies). `target_ts` is the file
    #: message that now holds the answer.
    TOOL_POST_MERGED = "tool_post_merged"
    #: Rewriting the tool's file message failed; the answer was posted as a
    #: separate reply instead, so it still reaches the thread.
    TOOL_POST_MERGE_FAILED = "tool_post_merge_failed"

    def __str__(self) -> str:
        return self.value


REQUEST_KIND = IncidentKind.REQUEST.value
PAYLOAD_KIND = IncidentKind.PAYLOAD.value
REVIEW_KIND = IncidentKind.REVIEW.value
WATCH_CHECKED_KIND = IncidentKind.WATCH_CHECKED.value
WATCH_FINISHED_KIND = IncidentKind.WATCH_FINISHED.value
WATCH_ABANDONED_KIND = IncidentKind.WATCH_ABANDONED.value

# Kinds that count as an "incident" for the reliability/quality rollups —
# REQUEST is excluded since it's the baseline traffic, not an incident.
BASELINE_KINDS: tuple[IncidentKind, ...] = (
    IncidentKind.REQUEST,
    IncidentKind.CAPABILITY,
    IncidentKind.PAYLOAD,
    IncidentKind.REVIEW,
    IncidentKind.WATCH_CHECKED,
    IncidentKind.WATCH_FINISHED,
    # One row per published reply carrying its watch disposition, not a fault
    # — counting it among incidents would swamp the real ones. metrics.py
    # reads these out separately for the NOWATCH 판정률 (J9).
    IncidentKind.WATCH_VERDICT,
)
#: Read by metrics.py as well -- a kind that is baseline here must not be
#: tallied as an incident there.
BASELINE_KIND_VALUES: frozenset[str] = frozenset(k.value for k in BASELINE_KINDS)
INCIDENT_KINDS: tuple[IncidentKind, ...] = tuple(k for k in IncidentKind if k not in BASELINE_KINDS)

# Every row `record()` writes always carries a kind (see `line` below), so
# this only guards a hand-edited or otherwise corrupted row.
_DEFAULT_KIND_FOR_MISSING = IncidentKind.REQUEST.value


def normalize_kind(kind: str | None) -> str:
    return kind or _DEFAULT_KIND_FOR_MISSING


class AuditLog(SqliteRepository):
    def __init__(
        self,
        db: Database,
        jsonl_path: Path,
        now: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(db)
        self._jsonl_path = jsonl_path
        self._now = now

    def record(
        self,
        kind: str,
        *,
        channel: str = "",
        thread_ts: str = "",
        **fields: Any,
    ) -> None:
        at = self._now()
        self._execute(
            "INSERT INTO audit (at, kind, channel, thread_ts, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (at, str(kind), channel, thread_ts, dump_json(fields)),
        )
        line = {"kind": str(kind), "channel": channel, "thread_ts": thread_ts, "at": at}
        line.update(fields)
        self._append_jsonl(line)

    def record_request(
        self,
        *,
        channel: str,
        thread_ts: str,
        message_ts: str,
        session_id: str,
        resumed: bool,
        model: str,
        effort: str,
        elapsed: float,
        ok: bool,
        first_reaction_sec: float | None = None,
        queue_wait_sec: float | None = None,
        usage: Mapping[str, Any] | None = None,
        user: str = "",
        # None means the engine didn't report a turn count (e.g. Codex) —
        # distinct from 0 turns, which is a real value.
        turns: int | None = None,
        **extra: Any,
    ) -> None:
        self.record(
            REQUEST_KIND,
            channel=channel,
            thread_ts=thread_ts,
            message_ts=message_ts,
            session_id=session_id,
            resumed=resumed,
            model=model,
            effort=effort,
            elapsed=elapsed,
            ok=ok,
            first_reaction_sec=first_reaction_sec,
            queue_wait_sec=queue_wait_sec,
            usage=dict(usage) if usage is not None else None,
            user=user,
            turns=turns,
            **extra,
        )

    def _append_jsonl(self, line: Mapping[str, Any]) -> None:
        self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._jsonl_path, "a", encoding="utf-8") as f:
            f.write(dump_json(line) + "\n")



#: Depth at which nesting is replaced by a placeholder. Well past anything an
#: audit field actually carries, so reaching it means the value is malformed.
