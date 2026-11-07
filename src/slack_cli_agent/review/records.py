# Finds the run record behind the answer a review was asked about. Rules
# follow bot.py find_answer_record as of 2026-10-19:
#
# 1. the posted message ts: answer_ts, or any chunk in answer_parts. This
#    runs channel-wide, before the thread filter: a DM or flat_reply answer
#    is a top-level message whose thread_ts is itself, not the question's;
# 2. body text, both sides normalized the same way, within the same thread;
# 3. the same thread's successful record logged right after the message
#    was posted.
#
# Steps 2 and 3 skip a record that carries a posted ts: it already names
# its own message, and that message is not the flagged one. Taking it
# anyway let a thread parent pick a later reply's body (B6 review).
#
# No match returns None. The pre-10-19 rule fell back to the thread's last
# successful record; the format review then overwrote the flagged message
# with a different answer's body (the di_order_v1 report vanished). A miss
# must stay a miss so the caller can refuse to rewrite.

from __future__ import annotations

import html
import json
import re
from typing import Any

from slack_cli_agent.observability.audit import REQUEST_KIND
from slack_cli_agent.storage.database import Database
from slack_cli_agent.storage.repository import SqliteRepository

_LOOKBACK_LIMIT = 4000
_PROBE_LEN = 40
_PROBE_MIN = 10
# Seconds from the message ts to the audit row's `at`. The row is written
# right after posting, so `at` sits a few seconds after the ts. A row
# written well after it belongs to a later answer in the same thread (a
# record-less watch notice followed by a reply 40s on was matched to that
# reply under the old 60s window). bot.py compares the run's end, which
# precedes the post, hence its window ran the other way.
_WRITTEN_BEFORE_MAX = 5.0
_WRITTEN_AFTER_MAX = 10.0


def norm_for_match(s: str | None) -> str:
    """Brings Slack text and a stored markdown answer to one shape.

    Slack text is HTML-escaped and keeps <@U...> mentions and <url|label>
    links; the stored answer has markdown links and emphasis. Normalizing
    only one side means an answer opening with a mention or link never
    matches.
    """
    s = html.unescape(s or "")
    s = re.sub(r"<@[A-Z0-9]+>|@U[A-Z0-9]{6,}", "", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = re.sub(r"<([^<>|]+)\|([^<>]+)>", r"\2", s)
    s = re.sub(r"<(https?://[^<>]+)>", r"\1", s)
    s = re.sub(r"[*_`~#>|]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _as_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _posted_ts(payload: dict[str, Any]) -> set[str]:
    """Every message ts the record says its answer was posted as."""
    out = {str(t) for t in payload.get("answer_parts") or () if t}
    if payload.get("answer_ts"):
        out.add(str(payload["answer_ts"]))
    return out


class AnswerRecordFinder(SqliteRepository):
    def __init__(self, database: Database) -> None:
        super().__init__(database)

    def find(
        self, channel: str, thread_ts: str, text: str, msg_ts: str | None = None
    ) -> dict[str, Any] | None:
        rows = self._fetch_all(
            "SELECT at, payload, thread_ts FROM audit "
            "WHERE kind = ? AND channel = ? ORDER BY id DESC LIMIT ?",
            (REQUEST_KIND, channel, _LOOKBACK_LIMIT),
        )
        recs: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or not payload.get("answer"):
                continue
            payload = dict(payload)
            payload["thread_ts"] = row["thread_ts"]
            payload["_at"] = row["at"]
            if msg_ts and msg_ts in _posted_ts(payload):
                return payload
            if row["thread_ts"] != thread_ts or _posted_ts(payload):
                continue
            recs.append(payload)

        probe = norm_for_match(text)[:_PROBE_LEN]
        if len(probe) >= _PROBE_MIN:
            for r in recs:
                if probe in norm_for_match(r["answer"]):
                    return r

        target = _as_float(msg_ts)
        if target is None:
            return None
        best: dict[str, Any] | None = None
        best_gap: float | None = None
        for r in recs:
            at = _as_float(r.get("_at"))
            if not r.get("ok") or at is None:
                continue
            gap = at - target
            if -_WRITTEN_BEFORE_MAX <= gap <= _WRITTEN_AFTER_MAX and (
                best_gap is None or abs(gap) < best_gap
            ):
                best, best_gap = r, abs(gap)
        return best
