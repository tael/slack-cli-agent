# Matches by body text first, since that's the only way to tell apart
# multiple answers in the same thread. Rich channels can have Slack-supplied
# text that's just a notification preview rather than the stored original,
# so on a miss this falls back to the thread's last successful record.

from __future__ import annotations

import html
import json
import re
from typing import Any

from slack_cli_agent.observability.audit import REQUEST_KIND
from slack_cli_agent.storage.database import Database
from slack_cli_agent.storage.repository import SqliteRepository

_LOOKBACK_LIMIT = 4000

_LINK_LABEL = re.compile(r"<([^<>|]+)\|([^<>]+)>")
_BRACKET = re.compile(r"<([^<>]+)>")


def clean_excerpt(text: str | None) -> str | None:
    # Slack's event API HTML-escapes text and already-rendered links come
    # as `<url|label>`; re-pasting that verbatim would double-wrap in
    # angle brackets. Unescape entities, keep only the link label, and
    # strip remaining brackets until nothing changes.
    if not text:
        return text
    out = html.unescape(text)
    out = _LINK_LABEL.sub(r"\2", out)
    prev = None
    while prev != out:
        prev = out
        out = _BRACKET.sub(r"\1", out)
    return out


class AnswerRecordFinder(SqliteRepository):
    def __init__(self, database: Database) -> None:
        super().__init__(database)

    def find(self, channel: str, thread_ts: str, text: str) -> dict[str, Any] | None:
        rows = self._fetch_all(
            "SELECT payload, thread_ts FROM audit "
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
            recs.append(payload)

        probe = (clean_excerpt(text or "") or "").strip()[:60]
        if probe:
            for r in recs:
                if probe in r["answer"]:
                    return r
        for r in recs:
            if r.get("thread_ts") == thread_ts and r.get("ok"):
                return r
        return None
