"""Re-checks a thread right before posting a reply that's already been composed.

Composing a reply takes time, and a human can post to the thread in
the meantime. Posting without checking would answer a question that's
already moot.

Slack reads go through the injected reliability.ports.HistoryReader
port rather than directly — this module only turns the result into a
transcript string.

2026-09-09 incident: when messages land in a thread in quick
succession, two code paths could consume the same one — the pre-send
recheck absorbed it in one run, then the queue processed the same
message again in a separate run, with no shared state between them.
Two of three replies ended up re-explaining the first. ThreadConsumption
records the timestamp already absorbed so both paths see the same
watermark.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.observability.notices import NoticeCatalog
from slack_cli_agent.reliability.ports import HistoryReader
from slack_cli_agent.slack.identity import BotIdentity
from slack_cli_agent.slack.mentions import CalledNames
from slack_cli_agent.slack.message_kind import MessageKind
from slack_cli_agent.slack.speaker import SpeakerNamer

from ..core.timezones import KST


class ThreadConsumption:
    """Tracks, per thread, the latest message timestamp already folded
    into a reply. A watermark, not a log — it only moves forward.
    Guarded by a lock since multiple runs can touch the same thread
    concurrently.
    """

    def __init__(self) -> None:
        self._consumed: dict[str, float] = {}
        self._lock = threading.Lock()

    def mark(self, thread_ts: str, ts: str | float | None) -> None:
        """Records ts as absorbed for this thread. Never moves the watermark backward."""
        if not ts:
            return
        with self._lock:
            cur = self._consumed.get(thread_ts, 0.0)
            self._consumed[thread_ts] = max(cur, float(ts))

    def consumed_ts(self, thread_ts: str) -> float:
        """Latest timestamp absorbed for this thread, or 0.0 if none yet."""
        with self._lock:
            return self._consumed.get(thread_ts, 0.0)

    def forget(self, thread_ts: str) -> None:
        """Clears the watermark for this thread, e.g. once no more requests will run against it."""
        with self._lock:
            self._consumed.pop(thread_ts, None)


class LateAddendumChecker:
    def __init__(
        self,
        history: HistoryReader,
        notices: NoticeCatalog,
        name_resolver: Callable[[str], str],
        settings: RuntimeSettings,
        identity: BotIdentity,
        bot_display_name: str,
        owner_user_id: str = "",
        owner_display_name: str = "",
        group_resolver: Callable[[str], str] | None = None,
    ) -> None:
        self._history = history
        self._notices = notices
        self._settings = settings
        # Speaker display delegates to the SpeakerNamer shared with
        # TranscriptBuilder. MessageKind.is_human already filters out
        # bot messages before they reach here, but we don't lean on
        # that — if it ever changes, this bot's own messages would
        # silently show up as "unknown bot" and nobody would notice
        # until they looked at the actual thread.
        self._speaker = SpeakerNamer(
            name_resolver=name_resolver,
            is_self=identity.is_self,
            bot_display_name=bot_display_name,
            owner_user_id=owner_user_id,
            owner_display_name=owner_display_name,
        )
        # Message classification lives in one place; duplicating it
        # here would let the criteria drift.
        self._kind = MessageKind()
        # Same shape as the transcript: body raw, direction in the head.
        # Two different shapes would read as two different people (sca-ddkf).
        self._called = CalledNames(name_resolver, group_resolver)

    def check(
        self,
        channel: str,
        thread_ts: str,
        ts: str | float,
        scope: str = "thread",
    ) -> tuple[str, str | None]:
        """Reads messages that landed after `ts` and formats them as a transcript.

        Returns (transcript, last message ts), or ("", None) if
        there's nothing new — including when the read itself is
        inconclusive. "Don't know" is treated the same as "nothing
        new" here, matching the original's behavior of just logging a
        warning and moving on.
        """
        base_ts = float(ts)
        limit = self._settings.history_max_msgs
        if scope == "channel":
            msgs = self._history.read_history(channel, base_ts, limit)
            if msgs is None:
                return "", None
        else:
            msgs = self._history.read_thread(channel, thread_ts, limit)

        lines: list[str] = []
        latest: str | None = None
        for m in msgs:
            if not self._kind.is_human(m):
                continue
            mts = float(m.get("ts", 0) or 0)
            if mts <= base_ts:
                continue
            text = (m.get("text") or "").strip()
            if not text or self._notices.is_notice(text):
                continue
            when = datetime.fromtimestamp(mts, KST).strftime("%H:%M:%S")
            who = self._speaker.speaker_of(m)
            called = self._called.called_in(text)
            head = f"{who} -> {', '.join(called)}" if called else who
            lines.append(f"[{when} {head}]\n{text}")
            latest = m.get("ts")
        return "\n\n".join(lines), latest


def late_addendum_prompt(addendum: str) -> str:
    """Wraps late-arriving thread messages into a prompt asking the
    model to fold them into the not-yet-posted reply.

    States up front, explicitly, that the earlier reply hasn't been
    posted and nobody has seen it. On 2026-09-11 this premise was
    missing: told that "if the new message is already covered, just
    point to it and stop," the model took that literally, discarded a
    completed answer, and pointed to content that didn't exist on
    Slack yet. That instruction only holds once the earlier reply is
    actually visible to the human.
    """
    return (
        "방금 답을 만들었고 아직 슬랙에 올리지 않았다.\n"
        "그 답은 사람이 본 적이 없다. 이 대화 어디에도 없다.\n"
        "올리기 직전에 이 스레드 아래로 말이 새로 달린 것을 발견했다.\n"
        "대괄호 안이 시각과 그 말을 한 사람이다.\n\n"
        "===== 새로 달린 말 =====\n\n"
        f"{addendum}\n\n"
        "===== 여기까지다 =====\n\n"
        "지금 낼 답 하나가 이 스레드에 올라가는 전부다.\n"
        "방금 만든 답의 내용을 빠짐없이 담고, 새로 달린 말에 대한 답을 더한다.\n"
        "앞 답이 이미 있다고 보고 그리로 가리키지 않는다. 가리킬 곳이 없다.\n"
        "목록, 표, 링크, 티켓 번호를 줄이거나 생략하지 않는다.\n"
        "방금 낸 답이 새로 달린 말과 어긋나는 부분만 그에 맞게 고친다.\n"
        "달라질 것이 없으면 방금 만든 답을 그대로 다시 낸다."
    )
