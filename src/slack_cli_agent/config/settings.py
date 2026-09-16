"""Runtime constants. Defaults come from production measurements; the reasoning is kept in comments."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class RuntimeSettings:
    # 300s would have cut 2 of 150 requests; the median was 34s.
    request_timeout_sec: float = 900
    slow_report_sec: float = 800
    # Catches wall-clock vs. monotonic clock drift — one case reported 43
    # minutes for what was actually 42 seconds.
    sleep_gap_suspect_sec: float = 30
    max_concurrent: int = 10

    slack_chunk: int = 3500
    markdown_block_limit: int = 12000

    session_ttl_hours: int = 24
    channel_session_ttl_days: int = 7

    catchup_window_sec: float = 7200
    catchup_max_window_sec: float = 86400
    catchup_thread_lookback_sec: float = 7 * 86400
    # Only covers Slack read lag; 3600 was measured wrong.
    catchup_grace_sec: float = 120

    # Calling faster than this returns ok with an empty list instead of a 429.
    history_min_interval_sec: float = 2.0
    history_read_tries: int = 3
    history_read_pause_sec: float = 2.0
    history_max_msgs: int = 40
    history_max_chars: int = 12000
    linked_thread_max: int = 3

    # 0 reconnects in 4h35m of normal operation; 128 in 22 minutes during an outage.
    socket_reconnect_limit: int = 4
    # The old 20-per-180s threshold never fired — the measured rate was 16.8.
    socket_error_limit: int = 8
    health_interval_sec: float = 30
    # How long to wait before re-checking bot identity after a failed
    # auth_test. Caching a failure forever would make a transient outage
    # look permanent for the process's whole lifetime; retrying on every
    # check would multiply API calls during an outage.
    identity_retry_interval_sec: float = 60
    # How long the worker sleeps when the queue is empty. 0 would busy-poll
    # and pin a core; this is the largest delay still imperceptible to a person.
    queue_idle_sleep_sec: float = 0.5
    shutdown_grace_sec: float = 330

    watch_check_interval_sec: float = 300
    watch_job_min_gap_sec: float = 300
    watch_job_max_age_sec: float = 24 * 3600

    roster_refresh_sec: float = 12 * 3600

    late_rewrite_min_ratio: float = 0.6
    late_rewrite_min_chars: int = 200

    progress_tick_sec: float = 3
    progress_idle_sec: float = 45

    # Stall detection fires at 3x the heartbeat interval.
    heartbeat_interval_sec: float = 5
    heartbeat_stale_sec: float = 15
    job_max_attempts: int = 3
    # How long finished jobs are kept before deletion. Must exceed
    # catchup_max_window_sec — if shorter, catch-up would treat an
    # already-answered message as unanswered and reply to it twice.
    job_retention_sec: float = 7 * 86400
    job_purge_interval_sec: float = 3600
    # A process running for days keeps receiving new attachments after
    # boot, so cleanup can't be a one-time pass at startup.
    attachment_cleanup_interval_sec: float = 3600
    catchup_retry_interval_sec: float = 30
    # How often a worker looks for connection epochs ingress left behind.
    connection_catchup_interval_sec: float = 30
    # Lease held while sweeping a claimed span. A measured sweep took 48s, so
    # a short lease would let a second worker grab a span still in progress.
    catchup_lease_sec: float = 600
    pending_report_flush_interval_sec: float = 30
    # 중단된 점검을 훑는 주기. 이미 죽은 점검이라 급하지 않고, 원장 조회 한 번이다.
    stale_review_sweep_interval_sec: float = 600
    # How often to check whether the learning batch should run. The check
    # itself is just a file read; schedule.py decides the actual daily run.
    learning_batch_interval_sec: float = 600
    # KST hour learning starts; before this, that day's records aren't all in yet.
    learning_run_hour: int = 22
    # Empty delegates to the running engine's own configured model. A shared
    # literal here was invalid on codex/gemini profiles (sca-dyb.10).
    learning_model: str = ""
    learning_effort: str = "medium"
    learning_thread_reply_limit: int = 50

    # Left empty rather than guessed — a guessed value would produce a
    # fabricated percentage.
    context_limit: Mapping[str, int] = field(default_factory=dict)

    # Channels allowed to show token usage. Defaults to empty so no
    # org-specific channel ID lives in code.
    owner_only_channels: frozenset[str] = frozenset()

    # Upper-bound estimate used to break down slow-request time. Actual
    # throughput varies per request, so dividing by this yields an
    # approximation, not a precise measurement.
    assumed_tokens_per_sec: float = 40

    # Tools handed to the engine. Read-only by default; a bot that needs to
    # write declares it in its profile rather than in code.
    base_tools: tuple[str, ...] = ("Read", "Grep", "Glob")
    owner_tools: tuple[str, ...] = ()

    # Line prefixes stripped from replies. Defaults to empty since the
    # actual prefix text is org-specific and doesn't belong in code.
    dropped_line_heads: tuple[str, ...] = ()

    def override(self, values: Mapping[str, Any]) -> RuntimeSettings:
        """Overrides only the fields present in `values`; unknown keys are
        ignored. Set-typed fields get converted, since JSON has no set
        literal and would otherwise arrive as a list.
        """
        known = set(self.__dataclass_fields__)
        taken = {k: v for k, v in values.items() if k in known}
        for key, value in taken.items():
            current = getattr(self, key)
            if isinstance(current, frozenset) and not isinstance(value, frozenset):
                taken[key] = frozenset(value)
            elif isinstance(current, tuple) and not isinstance(value, tuple):
                taken[key] = tuple(value)
        return replace(self, **taken)
