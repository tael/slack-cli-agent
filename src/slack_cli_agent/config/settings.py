"""Runtime constants. Defaults come from production measurements; the reasoning is kept in comments."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from ..prompt.composer import DEFAULT_SYSTEM_PROMPT_BUDGET_BYTES


@dataclass(frozen=True)
class RuntimeSettings:
    # 300s would have cut 2 of 150 requests; the median was 34s.
    request_timeout_sec: float = 900
    slow_report_sec: float = 800
    #: Same trigger for reviews, kept separate because a review normally
    #: spends minutes -- measured 150-169s on gemini (sca-2hf) -- so the two
    #: thresholds have different meanings even where the number matches.
    review_slow_report_sec: float = 800
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
    #: How many conversations_replies pages read_full_thread follows via next_cursor
    #: before stopping. A thread longer than one page (history_max_msgs) returns
    #: its replies oldest-first, so without this the most recent replies are
    #: dropped (F17, mametchi bot.py 되짚기 페이지네이션). Caps a runaway thread.
    history_max_pages: int = 10
    #: Cap on the assembled system prompt, in UTF-8 bytes. 0 or less turns the
    #: cap off. Where the default comes from: docs/지침-예산.md.
    system_prompt_budget_bytes: int = DEFAULT_SYSTEM_PROMPT_BUDGET_BYTES
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
    #: How many engine calls one watch may spend. Each check is a full engine
    #: call, so without a cap a condition watch spends max_age / min_gap of
    #: them -- 288 at the defaults. Reaching it hands the job to `expired`,
    #: which sends the owner a give-up report instead of going quiet
    #: (sca-2v2). 48 is four hours of checking at the default gap.
    watch_job_max_checks: int = 48
    #: Must stay above watch_job_max_age_sec: a job still in the queue needs
    #: its result file. Orphans only appear when registration never happened.
    watch_result_retain_sec: float = 48 * 3600
    watch_result_cleanup_interval_sec: float = 3600

    roster_refresh_sec: float = 12 * 3600
    #: 명부에 넣을 계정 핸들이 맞아야 하는 정규식. 기본값은 회사
    #: 워크스페이스의 이름.성 형식이고, 빈 문자열이면 형식을 안 본다 (sca-evt).
    roster_handle_pattern: str = r"\."

    #: owner_only_channels 선언과 실제 멤버를 대조하는 주기. 사람이 채널에
    #: 들어오는 일은 드물어 자주 볼 이유가 없고, 조회는 채널마다 한 번이다.
    owner_only_audit_interval_sec: float = 6 * 3600

    #: 사용량 확인 명령 주기. 원본과 같은 1시간이다.
    usage_check_interval_sec: float = 3600
    usage_check_timeout_sec: float = 60

    #: 주기 실행기 스레드가 살아 있는지 보는 주기. 죽는 일은 드물지만 죽으면
    #: 그 작업이 프로세스 수명 내내 멈춘다.
    service_watch_interval_sec: float = 300

    late_rewrite_min_ratio: float = 0.6
    late_rewrite_min_chars: int = 200

    #: B9: a separate model rewrites figurative wording before posting
    #: (bot.py:8162-8176). Off by default because it adds one model call per
    #: reply; a profile turns it on. Work-channel replies shorter than
    #: figurative_min_chars are skipped, chat-channel replies never are.
    figurative_check: bool = False
    figurative_min_chars: int = 80
    figurative_model: str = "sonnet"
    figurative_timeout_sec: float = 45

    #: E1-E8: answer a non-owner's DM with an isolated read-only turn
    #: instead of ignoring it (mametchi notify_dm_redirect). Off by default:
    #: a colleague sees the bot answer, so a profile opts in.
    dm_reply: bool = False
    #: Replies one person gets a day; the first DM past it gets one notice,
    #: later ones are only recorded. 0 means no cap. mametchi raised 10 to 50
    #: on 2026-10-27.
    dm_daily_limit: int = 50
    #: A person's probe count for the day that warns the owner (mametchi
    #: DM_PROBE_WARN_AT).
    dm_probe_warn_at: tuple[int, ...] = (2, 5)
    #: Empty uses the engine's own model. mametchi used opus: on 2026-10-14
    #: sonnet misread ordinary questions as probes.
    dm_model: str = ""
    #: KST hour the daily DM report goes out, for yesterday (mametchi 09시).
    dm_report_hour: int = 9
    #: How often the report job checks whether it is due; the check is a file read.
    dm_report_interval_sec: float = 600

    progress_tick_sec: float = 3
    progress_idle_sec: float = 45

    # Stall detection fires at 3x the heartbeat interval.
    heartbeat_interval_sec: float = 5
    heartbeat_stale_sec: float = 15
    #: How often a running worker sweeps for jobs a crashed worker left behind.
    #: Above heartbeat_stale_sec, so a worker that is merely slow to beat once
    #: isn't treated as dead.
    stale_reclaim_interval_sec: float = 60
    #: Lock wait for the DB calls a Slack event makes on the socket handler
    #: thread. Slack acks before the handler runs, so this does not affect that
    #: event's ack -- it caps how long one of the ten pool slots is held, which
    #: is what delays the events behind it (sca-9l1). With the store retries and
    #: their backoff the whole path stays about one second.
    ingress_lock_budget_sec: float = 0.25
    job_max_attempts: int = 3
    # How long finished jobs are kept before deletion. Must exceed
    # catchup_max_window_sec — if shorter, catch-up would treat an
    # already-answered message as unanswered and reply to it twice.
    job_retention_sec: float = 7 * 86400
    job_purge_interval_sec: float = 3600
    #: 끝난 연결 세대를 남겨 두는 기간. 캐치업 유예보다 길어야 한다 -- 세대를
    #: 지우면 그 구간의 공백을 회수할 근거가 사라진다 (sca-zb9).
    epoch_retention_sec: float = 7 * 86400
    epoch_purge_interval_sec: float = 3600
    # Admin command claims. Retention must exceed the catch-up window for the
    # same reason job rows do: a command whose reaction mark failed to post is
    # found again, and the claim row is the only thing left stopping a second
    # run (sca-8m5p).
    admin_claim_retention_sec: float = 7 * 86400
    # Failed claims are kept far longer: their mark is `x`, which catch-up
    # does not read as done, so the message stays findable until it ages out
    # of catchup_max_window_sec. This must outlast that by a wide margin.
    admin_claim_failure_retention_sec: float = 30 * 86400
    admin_claim_purge_interval_sec: float = 3600
    # How long a claim may stay RUNNING before it is treated as a process that
    # died mid-command. There is no heartbeat on a claim, so this is set well
    # past any real command rather than close to it -- taking a claim from a
    # process that is still working would post x on a command that then
    # succeeds (codex review).
    admin_claim_stale_sec: float = 3600
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
    # Tools a channel operator gets, named one by one (mametchi
    # OWNER_EXTRA_TOOLS). Not derived from owner_tools: that list names whole
    # servers, and an operator then got the owner's mail and cluster. Server
    # wildcards in any spelling (mcp__s__*, mcp__s, a bare server name) and
    # browser actions here are dropped. Empty by default, so an
    # operator gets nothing until the profile names it.
    operator_tools: tuple[str, ...] = ()
    # The lookup bundle every non-owner turn gets on top of base_tools
    # (mametchi ALLOWED_TOOLS + READ_SHELL_TOOLS): read-only MCP tools and
    # scoped read wrappers, named one by one. Server wildcards, browser actions
    # and the bare write tools are dropped. Empty by default; the tool names
    # are organisation configuration and come from the profile.
    readonly_tools: tuple[str, ...] = ()

    # Skills granted on every ordinary turn as `Skill(<name>)` entries, even in
    # a channel with skills disabled -- a read-only query skill a bot should
    # always be able to reach. The mechanism is common; which skills (if any)
    # get standing clearance is organisation configuration, so empty by default.
    always_skills: tuple[str, ...] = ()

    # Line prefixes stripped from replies. Defaults to empty since the
    # actual prefix text is org-specific and doesn't belong in code.
    dropped_line_heads: tuple[str, ...] = ()

    # J11/B1: when True, a reply carrying no `[[RECIPIENT:...]]` tag is held
    # while the tag alone is re-asked once with no tools; still none, and the
    # turn ends silent so the wrong person never receives a reply the model
    # never addressed. On by default: every turn's system prompt now demands
    # the tag (prompt/sections.py RECIPIENT_RULE). A profile can set it false.
    require_recipient: bool = True

    # S2 MCP 2단계 라우팅. 요청 문장으로 이 턴에 붙일 MCP 서버 묶음을 고른다.
    # {"always": [...], "groups": {이름: {"when": [키워드 또는 "re:정규식"],
    # "servers": [...]}}, "fallback": "all"}. 없으면 지금처럼 전부 붙인다.
    # 형식은 core/mcp_routing.py 가 읽는다.
    mcp_routing: Mapping[str, Any] | None = field(default=None, hash=False, compare=False)

    # S2 컨텍스트 임계 리셋. 세션 마지막 턴의 호출당 컨텍스트 토큰
    # (input + cache_read + cache_creation) 이 이 값 이상이면 다음 요청은 새
    # 세션으로 시작하고 스레드 기록을 다시 읽어 이어 간다. 0 이하는 끈다.
    # 기본은 끔이다. 첫 호출 바닥이 봇마다 달라(구치파치 S0 268k) 한 값으로 켜면
    # 이어 가야 할 턴이 모두 새 세션이 된다. 프로필에서 켠다.
    context_reset_tokens: int = 0

    # 기본 처리 흐름 (W4, 2026-11-10). True 면 Agent 가 열린 티어(소유자,
    # 운영자, 위임)의 작업 턴에 FLOW_NOTE 를 넣고 plan_gate 훅을 켠다.
    # 프로필에서 false 로 두면 지침과 훅이 함께 꺼진다.
    plan_flow: bool = True
    # plan_gate 가 계획 블록을 요구하기 시작하는 도구 호출 응답 순번. 이번
    # 요청에서 도구를 부른 응답이 이 값째인데 그 전 본문에 `[계획]` 이 없으면
    # 호출을 거부한다. 0 이하는 훅만 끈다. 엔진에는 PLAN_GATE 로 넘어간다.
    plan_gate_responses: int = 3

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
