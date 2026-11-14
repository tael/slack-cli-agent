"""Whether the main answer already landed in a watched thread, and the
transcript since registration — the two reads the watch's pre-post
duplicate check needs.

A watch's check turn and the thread it watches run independently. The
main answer can land in the gap between a check turn starting and its
report posting, so both reports end up in the channel minutes apart
(the 2026-09-30 23:17 duplicate `WatchJobChecker._superseded` guards).
That guard stays a no-op until something satisfies its
`ThreadActivityPort` — this is that something, reading Slack directly.
"""

from __future__ import annotations

import logging
from typing import Any

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.slack.identity import BotIdentity
from slack_cli_agent.slack.transcript import TranscriptBuilder

log = logging.getLogger(__name__)


class SlackThreadActivity:
    """`ThreadActivityPort` backed by conversations.replies.

    `replied_since` has to see the most recent replies — a duplicate is
    always the newest message — so it follows the cursor to the last
    page the same way the transcript read does, bounded by
    history_max_pages. The transcript itself is delegated to the shared
    TranscriptBuilder so the supersede judge reads the thread in the
    exact form every other turn sees it.
    """

    def __init__(
        self,
        client: Any,
        identity: BotIdentity,
        transcript: TranscriptBuilder,
        settings: RuntimeSettings,
    ) -> None:
        self._client = client
        self._identity = identity
        self._transcript = transcript
        self._settings = settings

    def replied_since(self, channel: str, thread_ts: str, since: float) -> bool:
        """Whether this bot posted in that thread after `since`.

        A read failure returns False — missing the check means the report
        posts, and losing a report to a flaky read is worse than the rare
        duplicate the whole check is meant to catch (same stance as
        `_superseded`'s own guard).
        """
        try:
            messages = self._read_replies(channel, thread_ts)
        except Exception as exc:  # noqa: BLE001 - a failed read must not drop the report
            log.warning(
                "감시 중복 판정용 스레드 조회 실패 : channel=%s thread=%s %s",
                channel, thread_ts, exc,
            )
            return False
        for msg in messages:
            if not self._identity.is_self(msg):
                continue
            try:
                if float(msg.get("ts", 0)) > since:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def transcript_since(self, channel: str, thread_ts: str, since: float) -> str:
        """Thread transcript from `since` onward, for the supersede verdict
        to read. before_ts is None so there is no upper bound — everything
        said after registration is in scope."""
        return self._transcript.thread_transcript(
            channel, thread_ts, before_ts=None, after_ts=since,
        )

    def _read_replies(self, channel: str, thread_ts: str) -> list[dict[str, Any]]:
        limit = self._settings.history_max_msgs
        msgs: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(max(1, self._settings.history_max_pages)):
            kwargs: dict[str, Any] = {"channel": channel, "ts": thread_ts, "limit": limit}
            if cursor:
                kwargs["cursor"] = cursor
            resp = self._client.conversations_replies(**kwargs)
            msgs.extend(resp.get("messages") or [])
            if not resp.get("has_more"):
                break
            cursor = (resp.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                break
        return msgs
