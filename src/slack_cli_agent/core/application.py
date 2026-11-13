"""Builds the full object graph for one Profile. Every component here takes
its collaborators via constructor injection, assembled in one place.

ingress() and worker() run as separate processes sharing one DB, so a slow
engine call can't block event intake.

Construction is lazy and cached — recreating a component on each call would
split the object that registered handlers from the one holding the
connection, so events would arrive but nothing would handle them.
"""

from __future__ import annotations

import logging
import os
import shlex
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from ..admin.admission import AdminAdmission
from ..admin.command import AdminContext
from ..admin.defaults import default_admin_commands
from ..admin.router import AdminRouter
from ..auth.delegation import DelegationLedger, DelegationRecorder
from ..auth.execution_policy import ExecutionPolicy
from ..auth.policy import DEFAULT_EFFORT, AccessPolicy, turn_tier
from ..auth.principal import Principal, TrustLevel, TurnTier
from ..auth.tools import ToolPolicy
from ..config.channel import (
    TOOL_ENFORCEMENT_STRICT,
    ChannelConfig,
    ChannelRegistry,
    channel_is_rich,
)
from ..config.channel import channel_slug as slug_for
from ..config.profile import EngineSpec, Profile
from ..config.settings import RuntimeSettings
from ..config.slug_migration import SLUG_MIGRATION_LOCK, ChannelSlugMigrator
from ..dm.daily_report import DmDailyReport
from ..dm.prompts import (
    DEFAULT_REPLY_SYSTEM,
    DEFAULT_REPORT_SYSTEM,
    REPLY_FILE,
    REPORT_FILE,
    load_prompt,
)
from ..dm.service import DmReplyService
from ..engine.base import CallOrigin, Engine, EngineRequest, EngineResponse
from ..engine.capability import ExecutionRequirements, ToolRestriction
from ..engine.mcp_health import broken_mcp_servers
from ..engine.registry import EngineRegistry, default_registry
from ..engine.runner import (
    DirectInvoker,
    EngineInvoker,
    EngineRunner,
    FallbackEngine,
    FallbackInvoker,
)
from ..engine.switcher import EngineSwitcher
from ..engine.tool_selection import ToolSelection
from ..engine.transcript import (
    SessionTranscriptReader,
    TranscriptReaderRegistry,
)
from ..guard.base import OutputGuard
from ..guard.dropline import ConfiguredLineDropGuard
from ..guard.figurative import MIN_CHARS_ENV as FIGURATIVE_MIN_CHARS_ENV
from ..guard.figurative import MODE_ENV as FIGURATIVE_MODE_ENV
from ..guard.figurative import MODEL_ENV as FIGURATIVE_MODEL_ENV
from ..guard.figurative import OUT_ENV as FIGURATIVE_OUT_ENV
from ..guard.figurative import TIMEOUT_ENV as FIGURATIVE_TIMEOUT_ENV
from ..guard.figurative import FigurativeGuard, is_chat_channel
from ..guard.mentions import (
    AddresseeGuard,
    BotMentionGuard,
    PeerExchangeLimiter,
    PlainMentionGuard,
)
from ..guard.pipeline import GuardPipeline
from ..guard.rewrite import RewriteLossGuard
from ..guard.watch import WatchPromiseGuard
from ..jobs.heartbeat import WorkerHeartbeat
from ..jobs.queue import SqliteJobQueue
from ..learning.analyzer import ProposalAnalyzer, ProposalBuilder
from ..learning.apply import LearningApplier
from ..learning.batch import LearningBatch
from ..learning.progress import ProgressStore
from ..learning.proposal import ProposalStore
from ..learning.reactions import ReactionCollector
from ..learning.render import ProposalRenderer
from ..learning.schedule import DailyBatchSchedule
from ..observability.app_snapshot import ApplicationSnapshotSource
from ..observability.audit import AuditLog, IncidentKind
from ..observability.model_cost import record_engine_spend
from ..observability.notices import NoticeCatalog
from ..observability.progress import ProgressCoordinator
from ..observability.request_timing import RequestTimingCalculator
from ..observability.response_archive import ResponseArchive
from ..observability.slow_report import (
    ElapsedDiagnostician,
    SessionContextCalculator,
    SlowReportFormatter,
    SlowRequestReporter,
    TimeBreakdownCalculator,
    UsageRowBuilder,
)
from ..observability.state_snapshot import StateSnapshotBuilder, StateSnapshotWriter
from ..plugin.base import BotPlugin
from ..plugin.hooks import (
    BackgroundJob,
    background_job_runner,
    background_runners,
    engine_slot,
    event_callbacks,
)
from ..plugin.loader import PluginLoader
from ..prompt.composer import PromptComposer, SystemPromptComposer
from ..prompt.knowledge import KnowledgeLoader
from ..prompt.library import PromptLibrary
from ..prompt.linked_threads import LinkedThreadNote
from ..prompt.sections import (
    AdvisorSection,
    AirflowSection,
    AskerSection,
    AskSection,
    AttachmentSection,
    AuthoritySection,
    ChannelModeSection,
    CompositionContext,
    FlowSection,
    KnowledgeSection,
    OwnerNoteSection,
    PeerRecoverySection,
    PersonaSection,
    PresentPeopleSection,
    PromptSection,
    ReviewFormatSection,
    RosterSection,
    SensitiveGuardSection,
    SilenceRuleSection,
    CacheBoundarySection,
    SlackFormatSection,
    ThreadCoordsSection,
    TrustedSection,
    TurnTierSection,
    WatchSection,
    WorkStyleSection,
)
from ..reliability.catchup import CatchupService, RetryStatus, StuckCause
from ..reliability.channel_join import join_registered_channels
from ..reliability.connection import ConnectionCatchupCoordinator
from ..reliability.dedup import DeduplicationTracker
from ..reliability.health import HealthMonitor, SelfRestarter, SocketErrorWatch
from ..reliability.outage import NetworkOutageEscalation, OutageTracker
from ..reliability.pending_report import PendingReportStore, assemble_recovery_report
from ..reliability.restart import ApplyRequestWatch
from ..reliability.startup import StartupCatchup
from ..reliability.watchjobs import WatchJob, WatchJobQueue
from ..reliability.watchresult import WatchOutcome, WatchResultReader
from ..reliability.watchrunner import (
    WatchJobChecker,
    extra_report_targets,
    extra_targets_note,
    watch_already_reported_prompt,
    watch_check_prompt,
    watch_supersede_prompt,
)
from ..render.blocks import BlockBuilder
from ..render.markdown import MarkdownConverter
from ..render.splitter import ContentSplitter
from ..render.verifier import SplitVerifier
from ..review.base import ReviewTarget, ReviewTask
from ..review.engine_adapter import ReviewEngineCaller
from ..review.format import FormatReviewTask
from ..review.ledger import ReviewLedger
from ..review.postmortem import PostmortemTask
from ..review.records import AnswerRecordFinder
from ..review.stale_reporter import StaleReviewReporter
from ..review.trace import DebugTraceTask
from ..session.awaiting import SqliteAwaitingStore
from ..session.manager import SessionManager
from ..session.store import SqliteSessionStore
from ..shadow.service import ShadowPlugin, ShadowService, TurnResult, shadow_pairs, tool_calls_in
from ..shadow.store import ShadowStore
from ..slack.assistant import DEFAULT_GREETING, AssistantPanel
from ..slack.attachments import AttachmentStore, DownloadResult
from ..slack.channel_names import ChannelNameResolver
from ..slack.credentials import CredentialResolver, owner_session_client, resolver_for
from ..slack.download import HttpDownloader
from ..slack.gate import ResponseGate
from ..slack.gateway import SlackGateway
from ..slack.generated_images import SlackGeneratedImagePoster
from ..slack.history import HistoryReader
from ..slack.history_port import SlackHistoryPort
from ..slack.identity import BotIdentity, SlackBotIdentity
from ..slack.late_addendum import LateAddendumChecker, ThreadConsumption
from ..slack.linked_threads import LinkedThreadReader
from ..slack.listener import EventListener
from ..slack.mentions import SelfMentionStripper
from ..slack.names import BotUserResolver, DisplayNameResolver, UserGroupNameResolver, UserNamer
from ..slack.origin_alive import SlackOriginAliveChecker
from ..slack.owner_only_audit import OwnerOnlyChannelAudit
from ..slack.participants import ThreadParticipants
from ..slack.progress import (
    FallbackProgressSink,
    SlackAgentStatusProgressSink,
    SlackProgressSink,
    SlackTaskCardProgressSink,
)
from ..slack.publisher import MessagePublisher
from ..slack.reactions import (
    DEBUG_TRACE_EMOJI,
    FORMAT_REVIEW_EMOJI,
    POSTMORTEM_EMOJI,
    ReactionMarker,
)
from ..slack.review_ports import (
    ReviewProgressDisplay,
    ReviewPublisher,
    SlackMessageLookup,
    SlackPermalinks,
    ThreadTranscriptPort,
)
from ..slack.roster import RosterBuilder
from ..slack.thread_activity import SlackThreadActivity
from ..slack.tool_post import SlackToolPostMerger
from ..slack.transcribe import Transcriber
from ..slack.transcript import TranscriptBuilder
from ..storage.admin_claims import AdminClaims
from ..storage.connection_epochs import SqliteConnectionEpochs
from ..storage.database import Database
from ..welcome.plugin import WelcomePlugin
from ..welcome.service import WelcomeService
from ..welcome.store import WelcomeStore
from .access import RequestAccess
from .channel_kind import is_direct_message_channel
from .context import RequestContext
from .errors import AgentError
from .ingress import IngressService
from .lifecycle import InflightCounter, InflightRegistry
from .periodic import PeriodicRunner
from .pipeline import RequestPipeline, _domain_applies, turn_reach
from .services import ServiceGroup
from .slots import ConcurrencySlots, FileSlots
from .spawn import ThreadTaskSpawner
from .sweeplog import SweepLog
from .timezones import KST
from .unregistered import UnregisteredNoticeLog
from .usage_check import UsageCheck, run_command
from .worker import Worker

#: 이 패키지 소스 리포 루트. 부검과 뇌 추적이 "코드는 여기" 라고 알려 주고 읽게 하는 위치다.
_CODE_ROOT = Path(__file__).resolve().parents[3]

log = logging.getLogger(__name__)

#: Local file tools a non-owner's DM turn does not get (mametchi DM_TOOLS).
_DM_LOCAL_FILE_TOOLS: frozenset[str] = frozenset({"Read", "Grep", "Glob"})

#: Server-name fragments of a browser server. A DM turn gets no browser tool,
#: reading ones included: the browser holds the owner's logins, and navigate
#: or tabs moves it (advisor_plan.md 32, mametchi bot.py:7194-7197 defect).
_DM_BROWSER_MARKS: tuple[str, ...] = ("browser", "playwright")


def _is_browser_tool(name: str) -> bool:
    if not name.startswith("mcp__"):
        return False
    server, _, tool = name[len("mcp__"):].partition("__")
    return any(mark in server for mark in _DM_BROWSER_MARKS) or tool.startswith("browser_")


def dm_requirements(tools: ToolSelection) -> ExecutionRequirements:
    """Strict, never downgraded: a DM reaches anyone in the workspace, so a
    fallback engine that cannot hold the tool list must refuse rather than
    run. read-only codex leaves file reads open (credentials, owner records)
    and gemini enforces nothing. An allowlist asks for the server-scoped level,
    which claude holds with MCP tools on the list; asking for exact would
    block claude itself."""
    restriction = tools.restriction
    if restriction is ToolRestriction.EXACT_ALLOWLIST:
        restriction = ToolRestriction.SERVER_SCOPED_ALLOWLIST
    return ExecutionRequirements(
        tool_restriction=restriction,
        downgradable_axes=frozenset(),
        policy=TOOL_ENFORCEMENT_STRICT,
    )

# One prompt file per channel mode, by naming convention — mode "agent_coach"
# maps to prompts/prompt_agent_coach.md without needing a lookup table.
# "chat" is the casual-banter mode: a channel where business/internal-system
# requests are declined but everything else (small talk, search, writing
# help) answers normally — mapped the same way, to prompts/prompt_chat.md.
DEFAULT_PROMPT = "PROMPT_DEFAULT"
KNOWN_MODES: tuple[str, ...] = ("private", "agent_coach", "api_helpdesk", "chat")

# Common knowledge files (prompt/knowledge.py's `_*.md` convention) named
# after one of these load on every request, regardless of whether the
# request's words match that file's own `when:` trigger. Empty by default —
# this is the common skeleton only; which skills (if any) get standing
# clearance is organisation configuration, not repo content.
ALWAYS_SKILLS: tuple[str, ...] = ()

# Loggers whose socket-connection warnings the connection watch attaches to.
SOCKET_LOGGERS: tuple[str, ...] = ("slack_sdk.socket_mode", "slack_bolt")
# bot.py:6939 delays the first usage check so one sample is stored before any judgement.
USAGE_CHECK_FIRST_DELAY_SEC = 60.0


def mode_prompt_names(modes: Sequence[str] = KNOWN_MODES) -> dict[str, str]:
    return {mode: f"PROMPT_{mode.upper()}" for mode in modes}



def _stuck_cause_text(status: RetryStatus) -> str:
    """Explains, per cause, why catch-up for a channel can't finish.

    The alert used to always blame an empty channel history, which was
    wrong once an unreadable long thread also kept a channel on the retry
    list (PR #124): the owner would look for a Slack outage that wasn't there.
    """
    if status.cause == StuckCause.UNREADABLE_THREAD:
        return (
            f"긴 스레드 {status.unreadable_threads}개를 끝까지 읽지 못했습니다.\n"
            "중간 페이지를 읽다 실패했거나 읽을 수 있는 페이지 수 한도를 넘었습니다.\n"
            "그 스레드에서 답을 기다리는 요청이 있어도 잡히지 않습니다.\n"
            "끝까지 읽은 나머지 스레드는 이번 점검에 반영했습니다."
        )
    if status.cause == StuckCause.UNKNOWN_IDENTITY:
        return (
            "봇 자신의 사용자 정보를 확인하지 못해 부름 여부를 판정하지 못합니다.\n"
            "그 채널에서 답을 기다리는 요청이 있어도 잡히지 않습니다."
        )
    return (
        "슬랙이 채널 기록을 계속 빈 목록으로 돌려줍니다.\n"
        "그 채널에서 답을 기다리는 요청이 있어도 잡히지 않습니다."
    )

class Application:
    """Builds and wires every component that makes up one bot."""

    def __init__(
        self,
        profile: Profile,
        client: Any,
        *,
        settings: RuntimeSettings | None = None,
        plugins: Sequence[BotPlugin] | None = None,
        database: Database | None = None,
        engine_registry: EngineRegistry | None = None,
        transcript_readers: TranscriptReaderRegistry | None = None,
        token_provider: Any = None,
        clock: Callable[[], float] = time.monotonic,
        owner_client: Any = None,
    ) -> None:
        self._profile = profile
        self._client = client
        # 소유자 사용자 세션 클라이언트. 소유자 턴의 링크 선읽기에만 쓴다(D5).
        self._owner_client = owner_client
        self._owner_channel_name_resolver: ChannelNameResolver | None = None
        # injected so tests can control identity-refresh timing
        self._clock = clock
        profile.paths.ensure()

        base = settings or RuntimeSettings()
        self._settings = base.override(profile.settings_override)

        self._plugins = tuple(plugins) if plugins is not None else self._load_plugins()
        self._token_provider = token_provider or (lambda: "")

        self._database = database or Database(profile.paths.database)
        self._database.migrate()

        self._registry = engine_registry or self._default_registry()
        # Register plugin engines even when the registry was supplied
        # externally — passing in plugins implies wanting their engines available.
        for plugin in self._plugins:
            for engine_class in plugin.engines():
                self._registry.register(engine_class)

        self._channels = ChannelRegistry(
            profile.paths.channels,
            on_slug_change=ChannelSlugMigrator(
                file_dirs=(profile.paths.knowledge, profile.paths.learned),
                tree_roots=(profile.paths.responses,),
                lock_path=profile.paths.root / SLUG_MIGRATION_LOCK,
            ).migrate,
        )
        self._names = DisplayNameResolver(client)
        self._user_namer: UserNamer | None = None
        self._group_names = UserGroupNameResolver(client)
        self._bot_users = BotUserResolver(client)
        # One bot-to-bot cap for the listener and catch-up (C3, C19); two
        # counters would let catch-up answer a mention the live path capped.
        # channels.json peer_turn_limit 로 채널별 한도를 바꾸거나 끈다 (0). 2026-11-08.
        self._peer_exchange = PeerExchangeLimiter(
            limit_for=lambda ch: getattr(self._channels.get(ch), "peer_turn_limit", None),
        )
        self._channel_name_resolver: ChannelNameResolver | None = None
        self._usage_check_run = run_command
        self._notices = NoticeCatalog()
        # Pre-send recheck and the send queue must see the same consumption
        # record, or the recheck absorbs a message the queue then resends.
        self.consumption = ThreadConsumption()
        # Worker and the state snapshot must share this counter, or the
        # state file always reports 0 in-flight.
        self.inflight = InflightCounter()
        # Worker records each handling request here and the state snapshot
        # reads it, so an operator can see what is in flight (B51). Shared
        # for the same reason as the counter above.
        self.active_inflight = InflightRegistry()
        self._started_at = time.time()
        self._shutting_down = False

        self._engine: Engine | None = None
        self._gateway: SlackGateway | None = None
        self._ingress: IngressService | None = None
        self._dm_reply: DmReplyService | None = None
        self._admin: AdminAdmission | None = None
        self._admin_claims: AdminClaims | None = None
        self._pipeline: RequestPipeline | None = None
        self._access_policy: AccessPolicy | None = None
        self._execution_policy: ExecutionPolicy | None = None
        self._tool_policy: ToolPolicy | None = None
        self._audit: AuditLog | None = None
        self._owner_dm_channel = ""
        self._queue: SqliteJobQueue | None = None
        self._reactions: ReactionMarker | None = None
        self._publisher: MessagePublisher | None = None
        self._review_tasks: dict[str, ReviewTask] | None = None
        self._welcome: WelcomePlugin | None = None
        self._welcome_service: WelcomeService | None = None
        self._welcome_caller: ReviewEngineCaller | None = None
        self._identity: SlackBotIdentity | None = None
        self._roster_builder: RosterBuilder | None = None
        self._roster_refresher: PeriodicRunner | None = None
        self._connection_watch: SocketErrorWatch | None = None
        self._health_runner: PeriodicRunner | None = None
        # 각 정리 작업의 연속 0건 횟수와 누적 삭제 수를 이 프로세스가 사는
        # 동안 들고 있는다. 매번 새로 만들면 그 수가 늘 1 로 보인다.
        self._job_purge_log = SweepLog("끝난 작업")
        self._epoch_purge_log = SweepLog("끝난 연결 세대")
        self._admin_claim_purge_log = SweepLog("끝난 관리 명령 점유")
        self._watch_result_log = SweepLog("감시 결과 파일")
        self._attachment_log = SweepLog("첨부")
        self._attachments: AttachmentStore | None = None
        self._response_archive: ResponseArchive | None = None
        self._progress: ProgressCoordinator | None = None
        self._learning_batch: LearningBatch | None = None
        self._proposals: ProposalStore | None = None
        self._learning_progress: ProgressStore | None = None
        self._catchup_service: CatchupService | None = None
        self._pending_report: PendingReportStore | None = None
        self._review_ledger_instance: ReviewLedger | None = None
        self._outage_tracker: OutageTracker | None = None
        # Decides when a sustained Slack-API outage is long enough to read as a
        # network-level (DNS/Wi-Fi) problem below the socket, so the recovery
        # report can say so (G59). Stateless, so a single shared instance.
        self._network_escalation = NetworkOutageEscalation()
        self._connection_epochs: SqliteConnectionEpochs | None = None
        self._transcript_reader_cache: dict[str, SessionTranscriptReader] = {}
        self._transcript_readers = transcript_readers or TranscriptReaderRegistry()
        self._invoker: EngineInvoker | None = None
        self._watch_jobs: WatchJobQueue | None = None
        self._watch_results: WatchResultReader | None = None
        self._closed = False

    @classmethod
    def from_profile(
        cls,
        profile: Profile,
        client: Any | None = None,
        env: Mapping[str, str] | None = None,
        resolver: CredentialResolver | None = None,
        **kwargs: Any,
    ) -> Application:
        """Creates the Slack client here; tests bypass this constructor path.

        The caller passes its own resolver when it already read a token: each
        resolver caches the credentials file separately, so building a second
        one lets the two tokens come from different versions of it.
        """
        resolver = resolver or resolver_for(profile, env=env)
        if client is None:
            from slack_sdk import WebClient  # lazy import so assembly tests don't need the SDK

            client = WebClient(token=resolver.bot_token())
        kwargs.setdefault("token_provider", resolver.bot_token)
        if "owner_client" not in kwargs:
            kwargs["owner_client"] = owner_session_client(
                resolver, owner_user_id=profile.owner_user_id
            )
        return cls(profile, client, **kwargs)

    def bot_token(self) -> str:
        return str(self._token_provider())

    def _load_plugins(self) -> tuple[BotPlugin, ...]:
        if not self._profile.plugins:
            return ()
        result = PluginLoader().load(self._profile.plugins)
        for failure in result.failures:
            log.error("플러그인을 읽지 못했다: %s (%s)", failure.module_path, failure.reason)
        return result.plugins

    @staticmethod
    def _default_registry() -> EngineRegistry:
        return default_registry()

    @property
    def engine_registry(self) -> EngineRegistry:
        return self._registry

    @property
    def profile(self) -> Profile:
        return self._profile

    @property
    def client(self) -> Any:
        return self._client

    @property
    def settings(self) -> RuntimeSettings:
        return self._settings

    @property
    def database(self) -> Database:
        return self._database

    @property
    def channels(self) -> ChannelRegistry:
        return self._channels

    @property
    def names(self) -> DisplayNameResolver:
        return self._names

    @property
    def plugins(self) -> tuple[BotPlugin, ...]:
        return self._plugins

    def channel_ids(self) -> list[str]:
        return list(self._channels.all())

    @property
    def engine(self) -> Engine:
        """Wraps the primary engine in a FallbackEngine when a fallback is configured."""
        if self._engine is None:
            primary = self._registry.create(
                self._profile.primary_engine.type, self._profile, self._settings
            )
            fallback_spec = self._profile.fallback_engine
            runner = self.engine_runner
            if fallback_spec is None:
                self._engine = primary
                self._invoker = DirectInvoker(
                    runner, primary,
                    notify=self._notify_owner if self._profile.owner_user_id else None,
                )
            else:
                secondary = self._registry.create(fallback_spec.type, self._profile, self._settings)
                fallback = FallbackEngine(
                    primary,
                    secondary,
                    EngineSwitcher(self._profile.paths.engine_state),
                    runner,
                    # 전환·복귀를 소유자 DM 으로 push (B24~B26). 소유자 미설정이면
                    # 발송만 생략하고 전환 자체는 그대로 일어난다.
                    notify=self._notify_owner if self._profile.owner_user_id else None,
                )
                self._engine = fallback
                # Passing the runner directly would bypass this class's fallback-switch logic.
                self._invoker = FallbackInvoker(fallback)
        return self._engine

    @property
    def engine_invoker(self) -> EngineInvoker:
        """One step of engine execution; callers don't need to know whether a fallback is active."""
        if self._invoker is None:
            # accessing .engine has the side effect of populating self._invoker
            _ = self.engine
        assert self._invoker is not None
        return self._invoker

    @property
    def engine_runner(self) -> EngineRunner:
        """No policy is pinned here: the runner asks each Engine for its own, so a
        fallback turn runs under the secondary's home rather than the primary's.
        """
        return EngineRunner(self._settings, audit=self.audit())

    @property
    def execution_policy(self) -> ExecutionPolicy:
        """One answer to 'what must the engine guarantee' for every caller
        that builds an EngineRequest outside the pipeline (sca-98k)."""
        if self._execution_policy is None:
            self._execution_policy = ExecutionPolicy()
        return self._execution_policy

    @property
    def access_policy(self) -> AccessPolicy:
        if self._access_policy is None:
            extensions = [e for p in self._plugins for e in p.access_extensions()]
            ledger_path = self._profile.delegation_ledger
            delegation = (
                DelegationLedger(
                    ledger_path,
                    self_user_id=lambda: self.identity.user_id,
                    own_name=self._profile.name,
                    delegator=self._profile.delegation_by,
                    reply_origin=(
                        self._profile.delegation_record
                        if self._profile.delegation_reply_only else None
                    ),
                )
                if ledger_path is not None else None
            )
            self._access_policy = AccessPolicy(
                self._profile, self._channels, extensions, delegation=delegation,
            )
        return self._access_policy

    @property
    def readable_dirs(self) -> tuple[Path, ...]:
        """Directories every engine call may read. One place so the watch
        check and the normal path cannot drift apart (sca-0ab).

        The watch results moved out of the work directory, so the check turn
        now reads them from outside its cwd (sca-vokt).

        attach_dir is deliberately not here: it holds every conversation's
        saved files under one root, so listing it would let one request's
        engine browse every other conversation's attachments. The pipeline
        adds only the specific subdirectory a request's own files live in
        (코덱스 8차 리뷰, sca-h2dr)."""
        return (
            self._profile.paths.persona,
            self._profile.paths.prompts,
            self._profile.paths.watch_out,
            *self._profile.extra_readable_dirs,
        )

    def tool_policy(self) -> ToolPolicy:
        if self._tool_policy is None:
            extensions = [e for p in self._plugins for e in p.access_extensions()]
            self._tool_policy = ToolPolicy(
                self._settings.base_tools,
                self._settings.owner_tools,
                extensions,
                channel_tools=self.access_policy.channel_tools_for,
                always_skills=self._settings.always_skills or ALWAYS_SKILLS,
                operator_level=self.access_policy.operator_level,
                operator_tools=self._settings.operator_tools,
                readonly_tools=self._settings.readonly_tools,
            )
        return self._tool_policy

    def _prompt_sections(self) -> list[PromptSection]:
        # Sections whose text depends only on the channel and the speaker's
        # tier go first, the ones carrying per-thread or per-request values
        # (speaker name, people, channel/thread_ts, watch run id, attachments)
        # go last. The prompt cache reuses the longest identical prefix, so a
        # thread_ts in the middle used to make every new thread rewrite the
        # rest of the prompt (S4, 2026-11-08). Knowledge is split the same
        # way: documents every turn carries go early, the ones a request's
        # `when:` words picked go after TurnTier. AirflowSection still follows
        # AuthoritySection, and SilenceRuleSection stays the closing note.
        sections: list[PromptSection] = [
            PersonaSection(),
            KnowledgeSection(part="fixed"),
            RosterSection(self._profile.roster_file),
            ChannelModeSection(mode_prompt_names(), default_prompt=DEFAULT_PROMPT),
            OwnerNoteSection(),
            PeerRecoverySection(self._profile.peer),
            SlackFormatSection(),
            ReviewFormatSection(),
            TrustedSection(),
            SensitiveGuardSection(),
            AuthoritySection(),
            AirflowSection(),
            AdvisorSection(),
            AskSection(),
            WorkStyleSection(),
            FlowSection(enabled=self._settings.plan_flow),
            TurnTierSection(),
            # Fixed text first, then what changes per speaker, then what
            # changes per request. 2026-11-12 (orchestration E): the selected
            # documents sat before WatchSection and AskerSection, so every
            # new request's prefix ended ~8 KB early, and 309 distinct
            # instruction blocks in 598 turns each rewrote the cached block.
            WatchSection(),
            CacheBoundarySection(enabled=self._settings.system_prompt_split),
            AskerSection(peer_bot_ids=self._profile.controller_user_ids),
            PresentPeopleSection(),
            ThreadCoordsSection(),
            AttachmentSection(),
            SilenceRuleSection(),
            # Documents picked by this request's `when:` words, last.
            KnowledgeSection(part="selected"),
        ]
        sections.extend(s for p in self._plugins for s in p.prompt_sections())
        return sections

    def _prompt_library(self) -> PromptLibrary:
        """The library every engine-facing guide reads, so a profile file's
        slots are filled the same way on the channel and the DM path."""
        placeholders = {"OWNER_MENTION": f"<@{self._profile.owner_user_id}>"}
        # The Airflow control tool is an org-specific path, so it comes from
        # the profile (mametchi bot.py prompt_text fills the same slot).
        if self._profile.airflow_ctl is not None:
            placeholders["AIRFLOW_CTL"] = str(self._profile.airflow_ctl)
        return PromptLibrary(
            self._profile.paths.prompts, placeholders=placeholders,
            file_placeholders=("NEVER_DISCLOSE",),
        )

    def _composer(self) -> PromptComposer:
        paths = self._profile.paths
        library = self._prompt_library()
        knowledge = KnowledgeLoader(
            paths.persona / "PERSONA.md", paths.knowledge,
            domain_file=self._profile.domain_file, learned_dir=paths.learned,
            local_dir=paths.local,
            always_skills=self._settings.always_skills or ALWAYS_SKILLS,
            wiki_dir=paths.wiki,
        )
        return SystemPromptComposer(
            library, knowledge, self._prompt_sections(),
            budget_bytes=self._settings.system_prompt_budget_bytes,
        )

    def _bot_mentions_allowed(self, channel: str) -> bool:
        """Read per request, not captured at build time, so turning the key on
        in channels.json applies without a restart like every other key."""
        config = self._channels.get(channel)
        return bool(config and config.bot_mentions)

    def _figurative_env(self, config: ChannelConfig | None) -> dict[str, str]:
        """Per-turn env for the Stop hook's figurative precompute
        (guard/figurative.Precompute). A fresh file path each turn: the guard
        reads and removes it, so no turn sees another turn's verdict."""
        out = Path(tempfile.gettempdir()) / "sca-figurative" / f"{uuid.uuid4().hex}.json"
        return {
            FIGURATIVE_OUT_ENV: str(out),
            FIGURATIVE_MODE_ENV: "chat" if is_chat_channel(config) else "work",
            FIGURATIVE_MIN_CHARS_ENV: str(self._settings.figurative_min_chars),
            FIGURATIVE_MODEL_ENV: self._settings.figurative_model,
            FIGURATIVE_TIMEOUT_ENV: str(self._settings.figurative_timeout_sec),
        }

    def _is_chat_channel(self, channel: str) -> bool:
        """Read per request, like `_bot_mentions_allowed`."""
        return is_chat_channel(self._channels.get(channel))

    def _is_bot_or_peer(self, user_id: str) -> bool:
        """A controller is a bot even when users_info fails. Both mention
        guards read this one verdict, so a peer that AddresseeGuard keeps at
        the head has already been through BotMentionGuard's channel check (B2)."""
        return user_id in self._profile.controller_user_ids or self._bot_users.is_bot(user_id)

    def _guards(self) -> GuardPipeline:
        guards: list[OutputGuard] = [
            PlainMentionGuard(),
            # Before AddresseeGuard: in a channel without `bot_mentions` a bot
            # mention at the head becomes a name here, so AddresseeGuard's
            # bot exception never lets one through that this guard would cut.
            BotMentionGuard(
                is_bot=self._is_bot_or_peer,
                display_name=self._names.resolve,
                allowed_in=self._bot_mentions_allowed,
                # A chat channel keeps a bot mention the request asked for.
                requested_only_in=self._is_chat_channel,
            ),
            AddresseeGuard(is_bot=self._is_bot_or_peer),
            WatchPromiseGuard(),
            RewriteLossGuard(self._settings),
            # safe to always include — a no-op when the drop-line list is empty
            ConfiguredLineDropGuard(self._settings),
        ]
        if not self._settings.figurative_after_post:
            # Last of the built-ins so it sees the body the other guards
            # settled on; a no-op unless `figurative_check` is on.
            guards.append(FigurativeGuard(self._settings, is_chat=self._is_chat_channel))
        guards.extend(g for p in self._plugins for g in p.output_guards())
        return GuardPipeline(guards)

    def gateway(self) -> SlackGateway:
        if self._gateway is None:
            self._gateway = SlackGateway(
                self._client,
                profile_name=self._profile.name,
                # Without this the worker never learns about a reconnect,
                # since only this process holds the socket.
                epoch_recorder=self.connection_epochs(),
            )
        return self._gateway

    def restart_guard_env(self) -> dict[str, str]:
        """What the self-restart guard needs to send a turn to the marker (H3):
        this profile's apply-request path and the launchd labels of its own
        services. Without it the guard's hint carries no path to write to."""
        request, _running = self._profile.paths.apply_requests
        return {
            "RESTART_GUARD_MARKER": str(request),
            # The base label is a prefix of every service label, so a kill
            # naming any of them (or the job family) is caught.
            "RESTART_GUARD_LABELS": self._profile.launch_label,
            # Exact service labels: launchctl is denied only against these, so
            # a turn can still register or kick its own side jobs
            # (local.deagent.autoupdate, 2026-11-08).
            "RESTART_GUARD_SERVICES": ",".join(
                f"{self._profile.launch_label}.{svc}" for svc in ("worker", "ingress")
            ),
        }

    def reactions(self) -> ReactionMarker:
        if self._reactions is None:
            self._reactions = ReactionMarker(self._client)
        return self._reactions

    def audit(self) -> AuditLog:
        """One instance shared by the pipeline and the publisher. Separate
        instances would split the same run across two jsonl handles."""
        if self._audit is None:
            self._audit = AuditLog(self._database, self._profile.paths.audit_log)
        return self._audit

    def publisher(self) -> MessagePublisher:
        if self._publisher is None:
            blocks = BlockBuilder(self._profile.display_name)
            self._publisher = MessagePublisher(
                client=self._client,
                settings=self._settings,
                markdown=MarkdownConverter(),
                splitter=ContentSplitter(self._settings, blocks),
                verifier=SplitVerifier(self._settings),
                blocks=blocks,
                bot_display_name=self._profile.display_name,
                audit=self.audit().record,
                backup_dir=self._profile.paths.format_originals,
            )
        return self._publisher

    def progress(self) -> ProgressCoordinator:
        """Progress display, built for every bot. Which channels actually get
        one is the `progress` flag in channels.json, read per request.

        The task card is primary and the rewrite sink is the fallback, so a
        workspace that rejects task_card still gets the plain step lines.
        The user id the streaming sink needed as a recipient is unused now."""
        if self._progress is None:
            self._progress = ProgressCoordinator(
                settings=self._settings,
                # 2026-11-07 김태일 지시 : 진행 카드 대신 마메치처럼 "앱이 작업 중"
                # 상태 표시만 쓴다. 상태가 거부되면 카드, 카드도 거부되면 줄 표시로 내려간다.
                sink_factory=lambda channel, thread_ts, _user: FallbackProgressSink(
                    lambda: SlackAgentStatusProgressSink(self._client, channel, thread_ts),
                    lambda: FallbackProgressSink(
                        # 끝난 카드가 "작업 완료" 로 답 위에 남아 답이 두 개처럼 읽혔다
                        # (2026-09-30 DE에이전트 시험). 끝나면 지워 스레드에 답만 남긴다.
                        lambda: SlackTaskCardProgressSink(
                            self._client, channel, thread_ts, self._profile.display_name,
                            keep_on_close=False,
                        ),
                        lambda: SlackProgressSink(
                            self._client, channel, thread_ts, self._profile.display_name,
                        ),
                    ),
                ),
                log_dir=self._profile.paths.progress,
                audit=self.audit().record,
            )
        return self._progress

    def user_namer(self) -> UserNamer:
        """What to call a user id. Every place that shows a name shares this
        one object, so the same person cannot appear under two names in one
        transcript (sca-inw8).

        The bot user ID it compares against is read per call, not here --
        resolving it during assembly would make wiring alone call Slack.
        """
        if self._user_namer is None:
            self._user_namer = UserNamer(
                self._names,
                identity=self.identity,
                bot_display_name=self._profile.display_name,
                owner_user_id=self._profile.owner_user_id,
            )
        return self._user_namer

    def _transcript_builder(self, client: Any = None) -> TranscriptBuilder:
        return TranscriptBuilder(
            client=self._client if client is None else client,
            settings=self._settings,
            notices=self._notices,
            name_resolver=self.user_namer(),
            group_resolver=self._group_names,
            identity=self.identity,
            bot_display_name=self._profile.display_name,
            owner_user_id=self._profile.owner_user_id,
        )

    def _late_addendum(self) -> LateAddendumChecker:
        # reuses the shared history adapter so history-fetch rate limiting stays centralized
        return LateAddendumChecker(
            self._history_port(),
            self._notices,
            self.user_namer(),
            self._settings,
            identity=self.identity,
            bot_display_name=self._profile.display_name,
            owner_user_id=self._profile.owner_user_id,
            group_resolver=self._group_names,
        )

    def _participants(self) -> ThreadParticipants:
        """Shares the _history_port adapter so history-fetch rate limiting stays
        centralized — otherwise Slack can start returning empty results.
        """
        return ThreadParticipants(
            self._history_port(),
            self.user_namer(),
            self.identity.user_id,
            limit=self._settings.history_max_msgs,
            group_resolver=self._group_names.resolve,
        )

    def _linked_threads(self) -> LinkedThreadNote:
        """Reuses _transcript_builder so a linked thread is formatted exactly
        like the current one — a second formatter here would drift.
        """
        owner_transcript = owner_names = None
        if self._owner_client is not None:
            owner_transcript = self._transcript_builder(self._owner_client)
            if self._owner_channel_name_resolver is None:
                self._owner_channel_name_resolver = ChannelNameResolver(
                    self._owner_client, self._channel_display_name
                )
            owner_names = self._owner_channel_name_resolver
        return LinkedThreadNote(
            LinkedThreadReader(
                self._transcript_builder(),
                self._channel_names(),
                max_links=self._settings.linked_thread_max,
                owner_transcript=owner_transcript,
                owner_channel_name=owner_names,
            )
        )

    def _channel_display_name(self, channel: str) -> str:
        config = self._channels.get(channel)
        return getattr(config, "name", "") or ""

    def _channel_names(self) -> ChannelNameResolver:
        if self._channel_name_resolver is None:
            self._channel_name_resolver = ChannelNameResolver(
                self._client, self._channel_display_name
            )
        return self._channel_name_resolver

    def _history_port(self) -> SlackHistoryPort:
        return SlackHistoryPort(
            HistoryReader(self._client, self._settings),
            self._client,
            max_pages=self._settings.history_max_pages,
        )

    def queue(self) -> SqliteJobQueue:
        if self._queue is None:
            self._queue = SqliteJobQueue(self._database)
        return self._queue

    def pipeline(self) -> RequestPipeline:
        if self._pipeline is None:
            self._pipeline = RequestPipeline(
                access_policy=self.access_policy,
                transcript_builder=self._transcript_builder(),
                prompt_composer=self._composer(),
                session_manager=SessionManager(
                    SqliteSessionStore(self._database),
                    self._settings,
                    # Session ID format is per engine. Minting it out here made
                    # all three happen to agree on UUID (sca-k6s).
                    new_session_id=self.engine.new_session_id,
                ),
                engine=self.engine,
                invoker=self.engine_invoker,
                guard_pipeline=self._guards(),
                post_corrector=(
                    FigurativeGuard(self._settings, is_chat=self._is_chat_channel)
                    if self._settings.figurative_check and self._settings.figurative_after_post
                    else None
                ),
                figurative_env=(
                    self._figurative_env
                    if self._settings.figurative_check and self._settings.figurative_in_stop_hook
                    and not self._settings.figurative_after_post
                    else None
                ),
                publisher=self.publisher(),
                audit=self.audit(),
                channels=self._channels,
                default_workdir=self._profile.work_root,
                light_workdir=self._profile.light_workdir,
                owner_user_id=self._profile.owner_user_id,
                reactions=self.reactions(),
                name_resolver=self.user_namer(),
                # AskerSection's bot-only branch (B1/B2). Shares the resolver the
                # output BotMentionGuard uses, so one lookup serves both.
                asker_is_bot=self._bot_users.is_bot,
                # J11: opt-in recipient enforcement. Off unless a profile's
                # settings turn it on, so a bot without RECIPIENT-tag persona
                # wiring degrades to "publish anyway" rather than to silence.
                require_recipient=self._settings.require_recipient,
                # S2: 요청 종류로 붙일 MCP 서버를 고른다. 없으면 전부.
                mcp_routing=self._settings.mcp_routing,
                # W4 plan_gate: 0 when the profile turned the flow off.
                plan_gate_responses=(
                    self._settings.plan_gate_responses if self._settings.plan_flow else 0
                ),
                mention_table=self._names.name_table,
                slow_reporter=self._slow_reporter(),
                # Resolved per request like the slow report: the engine that
                # answered decides the transcript format (S0 계측).
                request_timing=RequestTimingCalculator(self.transcript_reader),
                # built lazily rather than as a field — eagerly building it here would
                # look up the bot user ID, making assembly alone call out to Slack
                participants=lambda channel, thread_ts: self._participants().of(channel, thread_ts),
                linked_threads=lambda text, channel, attachments, as_owner: (
                    self._linked_threads().of(text, channel, attachments, as_owner=as_owner)
                ),
                late_addendum=self._late_addendum(),
                origin_alive=SlackOriginAliveChecker(self._client),
                tool_post_merger=SlackToolPostMerger(
                    self._client,
                    is_self=lambda msg: self.identity.is_self(msg),
                    update=lambda channel, ts, text, rich: self.publisher().update(
                        channel, ts, text, rich
                    ),
                    audit=self.audit().record,
                ),
                generated_images=SlackGeneratedImagePoster(
                    self._client, codex_home_for=self._codex_home_for,
                    audit=self.audit().record,
                ),
                consumption=self.consumption,
                watch_queue=self.watch_jobs(),
                watch_results=self.watch_results(),
                response_archive=self.response_archive(),
                tool_policy=self.tool_policy(),
                readable_dirs=self.readable_dirs,
                progress=self.progress(),
                # Persists the per-thread "waiting on a decision" flag so a
                # short approval ("좋아") arriving hours later still reads as an
                # answer and not a stray reaction — see session/awaiting.py.
                awaiting=SqliteAwaitingStore(self._database),
                restart_env=self.restart_guard_env(),
                # 워커에서 받아쓴다. 접수 경로에서 돌면 소켓 핸들러가 밀린다(G6).
                transcriber=Transcriber(),
                # A8: this bot hands work to another bot through the ledger
                # that bot reads. Off unless the profile names the file.
                delegation_recorder=(
                    DelegationRecorder(self._profile.delegation_record, delegator=self._profile.name)
                    if self._profile.delegation_record is not None else None
                ),
            )
        return self._pipeline

    def response_archive(self) -> ResponseArchive:
        """Uses KST for both the date and clock — in UTC, anything posted after
        9pm KST would land in the next day's file and get missed by that
        day's learning batch.
        """
        if self._response_archive is None:
            self._response_archive = ResponseArchive(
                self._profile.paths.responses, clock=lambda: datetime.now(KST),
            )
        return self._response_archive

    def learning_batch(self) -> LearningBatch:
        if self._learning_batch is None:
            paths = self._profile.paths
            archive = self.response_archive()
            analyzer = ProposalAnalyzer(
                self.engine_invoker,
                model=self._settings.learning_model or None,
                effort=self._settings.learning_effort,
                workdir=self._profile.work_root,
                bot_name=self._profile.display_name,
                audit=self.audit().record,
            )
            self._learning_batch = LearningBatch(
                archives=archive,
                reactions=ReactionCollector(
                    threads=self._history_port(),
                    thread_timestamps=archive.thread_timestamps,
                    channel_id_of=self._channel_id_of,
                    limit=self._settings.learning_thread_reply_limit,
                ),
                builder=ProposalBuilder(analyzer),
                store=self._proposal_store(),
                progress=self._learning_progress_store(),
                applier=LearningApplier(paths.learned, self._profile.display_name),
                renderer=ProposalRenderer(),
                notify=self._notify_admin,
                clock=lambda: datetime.now(KST),
            )
        return self._learning_batch

    def _channel_id_of(self, channel_name: str) -> str | None:
        for channel_id, config in self._channels.all().items():
            if config.name == channel_name:
                return channel_id
        # owner DM has no entry in the channel registry (it isn't a public
        # channel), so it's pulled from the profile instead
        if channel_name == "dm":
            return self._profile.owner_dm or None
        return None

    def _learning_day_done(self, day: str) -> bool:
        """Treats a day as done via the store's marker rather than by checking
        for a proposal file — a day where the proposal saved but applying it
        failed would otherwise never rerun.
        """
        return self._proposal_store().is_done(day)

    def _learning_progress_store(self) -> ProgressStore:
        if self._learning_progress is None:
            # Its own directory: ProposalStore.latest() scans the proposal
            # directory, and a progress file there read as a proposal.
            self._learning_progress = ProgressStore(self._profile.paths.proposals / "progress")
        return self._learning_progress

    def _proposal_store(self) -> ProposalStore:
        if self._proposals is None:
            self._proposals = ProposalStore(self._profile.paths.proposals)
        return self._proposals

    def _learning_run_hour(self) -> int:
        """Learning start hour; falls back to the default when configured
        out of [0, 23] — silently accepting an out-of-range value would mean
        the schedule condition never fires.
        """
        hour = self._settings.learning_run_hour
        if 0 <= hour <= 23:
            return hour
        fallback = RuntimeSettings().learning_run_hour
        log.warning("learning_run_hour 값이 범위 밖이다(%s). %s 시로 되돌린다", hour, fallback)
        return fallback

    def learning_schedule(self) -> DailyBatchSchedule:
        return DailyBatchSchedule(
            clock=lambda: datetime.now(KST),
            run_hour=self._learning_run_hour(),
            is_done=self._learning_day_done,
            unsettled_days=self._learning_progress_store().unsettled_days,
            is_waiting=self._learning_progress_store().is_waiting,
        )

    def _learning_batch_tick(self) -> None:
        day = self.learning_schedule().due_day()
        if day is None:
            return
        report = self.learning_batch().run(day)
        if not report.ran:
            log.info("%s 학습 배치를 건너뛰었다 : %s", day, report.reason)
            return
        log.info("%s 학습 배치를 마쳤다. 반영 %s, 알림 %s", day, dict(report.applied), report.notified)

    def learning_batch_runner(self) -> PeriodicRunner:
        """Ticks frequently, but the batch itself only runs once a day —
        each tick just cheaply checks a completion marker; DailyBatchSchedule
        owns the date logic.
        """
        return PeriodicRunner(
            self._learning_batch_tick,
            self._settings.learning_batch_interval_sec,
            name="learning_batch",
        )

    def stale_reclaim_runner(self, worker: Worker) -> PeriodicRunner:
        """Requeues jobs whose worker stopped sending heartbeats.

        The startup reclaim only covers the process that just restarted. With
        more than one worker, a crashed worker's RUNNING jobs would sit there
        until the next restart of that same worker, which may never come
        (sca-vlx). Marker updates go through Worker.reclaim so a periodic
        sweep leaves the same reaction state a startup sweep does.
        """
        return PeriodicRunner(
            worker.reclaim,
            self._settings.stale_reclaim_interval_sec,
            name="stale_reclaim",
        )

    def job_purge_runner(self) -> PeriodicRunner:
        """Completed/failed job rows also serve as a dedup record, so
        retention must outlast the catch-up window — otherwise an
        already-answered message could get reclaimed as unanswered.
        """
        return PeriodicRunner(
            self._purge_finished_jobs,
            self._settings.job_purge_interval_sec,
            name="job_purge",
        )

    def _purge_finished_jobs(self) -> None:
        removed = self.queue().purge_finished(time.time() - self._settings.job_retention_sec)
        self._job_purge_log.record(removed)

    @property
    def _claim_owner(self) -> str:
        """Only for diagnosis -- the claim itself is decided by the row, not
        this value. Has to differ per process to say which one died."""
        return f"pid:{os.getpid()}"

    def admin_claims(self) -> AdminClaims:
        if self._admin_claims is None:
            self._admin_claims = AdminClaims(self._database)
        return self._admin_claims

    def admin_claim_purge_runner(self) -> PeriodicRunner:
        """Closed claims are a dedup record, so they outlive the catch-up
        window the same way job rows do (sca-8m5p)."""
        return PeriodicRunner(
            self._purge_admin_claims,
            self._settings.admin_claim_purge_interval_sec,
            name="admin_claim_purge",
        )

    def _purge_admin_claims(self) -> None:
        now = time.time()
        removed = self.admin_claims().purge(
            now - self._settings.admin_claim_retention_sec,
            failures_before=now - self._settings.admin_claim_failure_retention_sec,
        )
        self._admin_claim_purge_log.record(removed)

    def admin_claim_reclaim_runner(self) -> PeriodicRunner:
        """A claim left RUNNING by a process that died blocks its message
        forever: no mark was posted, so catch-up keeps finding it, and the
        claim stops anyone from running it (sca-8m5p)."""
        return PeriodicRunner(
            self._reclaim_stale_admin_claims,
            self._settings.admin_claim_purge_interval_sec,
            name="admin_claim_reclaim",
        )

    def _reclaim_stale_admin_claims(self) -> None:
        stale = self.admin_claims().reclaim_stale(
            time.time() - self._settings.admin_claim_stale_sec
        )
        for claim in stale:
            log.warning("관리 명령이 처리 도중 끊겼다 : %s:%s : %s", claim.channel, claim.ts, claim.owner)
            self.reactions().mark_failed(claim.channel, claim.ts)

    def epoch_purge_runner(self) -> PeriodicRunner:
        """Closed connection epochs are only a record of a gap already caught
        up, but purge_done had no caller at all, so the table only grew
        (sca-zb9).
        """
        return PeriodicRunner(
            self._purge_done_epochs,
            self._settings.epoch_purge_interval_sec,
            name="epoch_purge",
        )

    def _purge_done_epochs(self) -> None:
        removed = self.connection_epochs().purge_done(
            time.time() - self._settings.epoch_retention_sec
        )
        self._epoch_purge_log.record(removed)

    def watch_jobs(self) -> WatchJobQueue:
        # cached and shared so registration, checking, and status reporting all
        # see the same instance — recreating it would work (storage is in the
        # DB) but would hide which paths touch it
        if self._watch_jobs is None:
            self._watch_jobs = WatchJobQueue(self._database)
        return self._watch_jobs

    def watch_results(self) -> WatchResultReader:
        """One reader for the whole bot. The directory is not workdir-relative
        any more, so there is a single place for the prompt note, the reader
        and the cleanup to agree on (sca-vokt)."""
        if self._watch_results is None:
            self._watch_results = WatchResultReader(self._profile.paths.watch_out)
            self._watch_results.ensure_dir()
        return self._watch_results

    def thread_activity(self) -> SlackThreadActivity:
        """The pre-post duplicate check's two reads. Shares identity and the
        transcript builder with the rest of the bot so "did I already reply"
        and "what was said" use the same self-detection and formatting."""
        return SlackThreadActivity(
            client=self._client,
            identity=self.identity,
            transcript=self._transcript_builder(),
            settings=self._settings,
        )

    def watch_checker(self) -> WatchJobChecker:
        # owner notification needs an owner to send to; the DM channel itself
        # is resolved at send time (see owner_dm_channel)
        return WatchJobChecker(
            queue=self.watch_jobs(),
            run_check=self._watch_run_check,
            results=self.watch_results(),
            publisher=self.publisher(),
            channels=self._channels,
            settings=self._settings,
            reactions=self.reactions(),
            notify_owner=self._notify_owner if self._profile.owner_user_id else None,
            # checks/last_run are current state only. Without this there is no
            # record that a watch ever ran (sca-j3d).
            audit=self.audit(),
            # The pre-post duplicate check. Both collaborators together, or the
            # check stays off and the report posts as always (F40/F41): the main
            # answer could land in the gap between a check starting and its
            # report posting, and both would reach the channel.
            thread_activity=self.thread_activity(),
            run_supersede_check=self._run_watch_supersede_check,
            # The already-reported verdict, in its own tool-less turn: the
            # thread text is not the registrant's, so it stays out of the
            # check turn that has read tools (C6 review).
            run_reported_check=self._run_watch_reported_check,
            # A question deleted between registration and report would leave
            # the report in a thread that is no longer the asker's (C6).
            origin_alive=SlackOriginAliveChecker(self._client),
        )

    def watch_result_cleanup_runner(self) -> PeriodicRunner:
        # A run_id is issued per request, so work started without a watch tag --
        # or whose registration failed -- leaves result files nobody reads
        # (sca-y6g). No completion path passes those, so this is the only trigger.
        return PeriodicRunner(
            self._watch_result_cleanup_tick,
            self._settings.watch_result_cleanup_interval_sec,
            name="watch_result_cleanup",
        )

    def _watch_result_cleanup_tick(self) -> None:
        removed = self.watch_results().cleanup(
            older_than_sec=self._settings.watch_result_retain_sec
        )
        self._watch_result_log.record(removed)

    def watch_runner(self, hold: Callable[[], bool] | None = None) -> PeriodicRunner:
        # without this runner, watch jobs get registered but never checked.
        # `hold` skips a tick: a check is an engine turn outside the claim
        # loop, and one started after an apply request is cut by the restart.
        check = self.watch_checker().check_once

        def tick() -> None:
            if hold is None or not hold():
                check()

        return PeriodicRunner(
            tick,
            self._settings.watch_check_interval_sec,
            name="watch_jobs",
        )

    def pending_report(self) -> PendingReportStore:
        if self._pending_report is None:
            self._pending_report = PendingReportStore(
                path=self._profile.state_dir / "pending_report.json",
                sender=self._post_owner_dm,
            )
        return self._pending_report

    def pending_report_runner(self) -> PeriodicRunner:
        # retries on a timer rather than only at startup — a report saved
        # after startup would otherwise sit in the file until the next restart
        return PeriodicRunner(
            self.pending_report().flush,
            self._settings.pending_report_flush_interval_sec,
            name="pending_report",
        )

    def owner_dm_channel(self) -> str:
        """Resolves the owner's DM channel, opening it if the profile doesn't
        name one. Posting to an empty channel id fails with channel_not_found,
        and the pending-report retry then repeats that failure on every tick."""
        if self._profile.owner_dm:
            return self._profile.owner_dm
        if self._owner_dm_channel:
            return self._owner_dm_channel
        if not self._profile.owner_user_id:
            return ""
        response = self._client.conversations_open(users=self._profile.owner_user_id)
        channel = ((response or {}).get("channel") or {}).get("id") or ""
        self._owner_dm_channel = str(channel)
        return self._owner_dm_channel

    def _post_owner_dm(self, text: str) -> None:
        channel = self.owner_dm_channel()
        if not channel:
            raise AgentError("소유자 DM 방을 찾지 못했다")
        self.publisher().post(channel, "", text, False)

    def _notify_owner(self, text: str) -> bool:
        """Notifies the owner's DM; returns whether it actually landed.

        On failure, saves the report to disk instead of just logging —
        this matters most exactly when reporting a restart, since the
        socket tends to be unstable at that moment. Returns False on the
        fallback so callers can't mistake a queued report for a delivered one.
        """
        try:
            self._post_owner_dm(text)
        except Exception as exc:  # noqa: BLE001 — failures range from Slack SDK errors to network errors; any of them should fall through to saving the report
            log.warning("소유자 알림 발송 실패, 보고를 남긴다 : %s", exc)
            self.pending_report().save(text)
            return False
        return True

    def _notify_admin(self, text: str) -> bool:
        """Posts a routine operational report to the admin channel
        (troubleshoot_channel), falling back to the owner DM.

        2026-11-13 김태일 지시 : 학습 보고, DM 일일 점검, 재기동 결과 세 보고를
        admin 채널로 보낸다. troubleshoot_channel 이 비었거나 발송이 실패하면
        기존 소유자 DM 경로(실패 시 pending_report 저장)로 넘겨 보고를 잃지 않는다.
        """
        channel = self._profile.troubleshoot_channel
        if channel:
            try:
                self.publisher().post(channel, "", text, False)
                return True
            except Exception as exc:  # noqa: BLE001 — any Slack/network failure falls back to the owner DM
                log.warning("admin 채널 보고 발송 실패, 소유자 DM 으로 보낸다 : %s", exc)
        return self._notify_owner(text)

    def _watch_run_check(self, job: WatchJob, outcome: WatchOutcome) -> EngineResponse:
        """Runs the check with a fresh session ID — the check happens well
        after registration, so the original conversation session may have
        expired, and resuming a dead session would just fail. Trust level is
        carried over from registration; downgrading it here would break
        lookups for jobs registered under owner trust.
        """
        principal = Principal(
            user_id=self._profile.owner_user_id if job.trust is TrustLevel.OWNER else "",
            channel=job.channel,
            trust=job.trust,
            is_direct_message=is_direct_message_channel(job.channel),
        )
        config = self._channels.get(job.channel)
        workdir = _watch_workdir(job, config, self._profile.work_root)
        결과파일 = self.watch_results().path_for(job.run_id)
        prompt = watch_check_prompt(job.condition, outcome, str(결과파일) if 결과파일 else "")
        # The runner posts to any other thread the condition names; say so,
        # or the read-only turn reports that it lacked the right (job 35).
        prompt += extra_targets_note(extra_report_targets(job))
        watch_tools = ToolSelection.from_names(
            self.tool_policy().tool_list_for(
                principal, prompt=prompt, readonly=True, skills_enabled=False,
            ),
            lookups=self.tool_policy().lookups_for(principal, readonly=True),
        )
        request = EngineRequest(
            prompt=prompt,
            system_prompt="",
            # Left empty on purpose: the engine that ends up running this
            # check mints one in its own format (sca-56y).
            session_id=None,
            resume=False,
            model=self.access_policy.model_for(principal),
            effort=self.access_policy.effort_for(principal, prompt),
            workdir=workdir,
            # readonly: the check only looks. Owner extras and Skill would let
            # this turn start new work, which `watch_check_prompt` forbids.
            # Left empty before, and an empty list is not a ban -- it is the
            # absence of one, so nothing would be closed (sca-6ewc).
            tools=watch_tools,
            requirements=self.execution_policy.requirements_for(
                config=config, tools=watch_tools,
            ),
            # This check turn belongs to one watch run. Without it the audit
            # cannot say which watch a capability record came from (sca-4ol).
            request_id=f"watch-{job.id}-{job.run_id}",
            readable_dirs=self.readable_dirs,
            trust_level=job.trust,
            # readonly tools, so the readonly tier (A12 review): without it
            # the check turn had no tier note and no tier in its audit.
            turn_tier=TurnTier.READONLY.value,
        )
        return self.engine_invoker.invoke(
            self._with_watch_prompt(request, principal, config, prompt),
            CallOrigin.BACKGROUND,
        )

    def _with_watch_prompt(
        self, request: EngineRequest, principal: Principal,
        config: ChannelConfig | None, prompt: str,
    ) -> EngineRequest:
        reads_only, acting = turn_reach(self.engine, request)
        return replace(request, system_prompt=self._composer().compose(CompositionContext(
            principal=principal,
            prompt=prompt,
            channel_mode=config.mode if config else "default",
            channel_slug=slug_for(principal.channel, config),
            is_rich=channel_is_rich(config),
            chat_level=config.chat if config else "normal",
            # This turn only looks: the registration guidance would tell it how
            # to start new work, which watch_check_prompt forbids (sca-ejy).
            watch_check=True,
            turn_tier=request.turn_tier,
            turn_reads_only=reads_only,
            turn_acting_tools=acting,
        )))

    def _run_watch_supersede_check(
        self, job: WatchJob, transcript: str, draft: str
    ) -> EngineResponse:
        """The verdict turn for the pre-post duplicate check. Pure judgment:
        the transcript and the draft are both in the prompt, so this turn
        reads nothing and writes nothing -- it only answers with one tag. No
        tools for that reason, which also keeps it from starting new work the
        way `watch_check_prompt` forbids for the check turn next to it.
        """
        return self._run_watch_verdict(
            job, watch_supersede_prompt(job.condition, transcript, draft),
            f"watch-supersede-{job.id}-{job.run_id}",
        )

    def _run_watch_reported_check(self, job: WatchJob, transcript: str) -> EngineResponse:
        """The already-reported verdict before a check, tool-less for the same
        reason as the duplicate check: the transcript is anyone's text."""
        return self._run_watch_verdict(
            job, watch_already_reported_prompt(job.condition, transcript),
            f"watch-reported-{job.id}-{job.run_id}",
        )

    def _run_watch_verdict(self, job: WatchJob, prompt: str, request_id: str) -> EngineResponse:
        principal = Principal(
            user_id=self._profile.owner_user_id if job.trust is TrustLevel.OWNER else "",
            channel=job.channel,
            trust=job.trust,
            is_direct_message=is_direct_message_channel(job.channel),
        )
        config = self._channels.get(job.channel)
        workdir = _watch_workdir(job, config, self._profile.work_root)
        # forbid_all, not from_names([]): no names means unrestricted, which
        # handed a non-owner job's thread to a turn with every tool open.
        no_tools = ToolSelection.forbid_all()
        request = EngineRequest(
            prompt=prompt,
            system_prompt="",
            session_id=None,
            resume=False,
            model=self.access_policy.model_for(principal),
            # The condition, not the prompt: the prompt carries thread text,
            # and a participant's "ultrathink" would raise the effort.
            effort=self.access_policy.effort_for(principal, job.condition),
            workdir=workdir,
            tools=no_tools,
            requirements=self.execution_policy.requirements_for(
                config=config, tools=no_tools,
            ),
            request_id=request_id,
            readable_dirs=self.readable_dirs,
            trust_level=job.trust,
            turn_tier=TurnTier.READONLY.value,
        )
        # Same composition as the check turn: registration guidance would tell
        # it how to start new work, which this judgment turn has no business doing.
        return self.engine_invoker.invoke(
            self._with_watch_prompt(request, principal, config, prompt),
            CallOrigin.BACKGROUND,
        )

    def mark_shutting_down(self) -> None:
        # without this flag, anything reading the state file can't tell a
        # stopped process from one that's shutting down
        self._shutting_down = True

    def _snapshot_source(self) -> ApplicationSnapshotSource:
        return ApplicationSnapshotSource(
            inflight=self.inflight,
            active=self.active_inflight,
            queue=self.queue(),
            # The snapshot is written by the worker, which has no socket. Its
            # own log watch would count zero forever and that reads as "no
            # errors" (sca-qi5.3). Reconnects come from the ledger ingress writes.
            socket_watch=None,
            connection_epochs=self.connection_epochs(),
            watch_jobs=self.watch_jobs(),
            is_shutting_down=lambda: self._shutting_down,
            started_at=self._started_at,
        )

    def state_snapshot_writer(self) -> StateSnapshotWriter:
        return StateSnapshotWriter(
            self._profile.paths.state_snapshot,
            StateSnapshotBuilder(self._snapshot_source()),
        )

    def state_snapshot_runner(self) -> PeriodicRunner:
        # shares the health-check interval; writing more often wouldn't help
        # since nothing reads the snapshot that fast
        return PeriodicRunner(
            self.state_snapshot_writer().write,
            self._settings.health_interval_sec,
            name="state_snapshot",
        )

    def _catchup(self) -> CatchupService:
        # cached — this object holds unfinished channels and retry counts;
        # rebuilding it each round would empty that state and make the retry
        # interval always look like the first round
        if self._catchup_service is None:
            self._catchup_service = CatchupService(
                history=self._history_port(),
                gate=ResponseGate(),
                notices=self._notices,
                settings=self._settings,
                identity=self.identity,
                attachments=self.attachments(),
                # The listener's C16/C17 checks use this lookup too (C3).
                is_bot=self._bot_users.is_bot,
                bot_user_of=self._bot_users.user_of_bot,
                peer_limiter=self._peer_exchange,
            )
        return self._catchup_service

    def concurrency_slots(self) -> ConcurrencySlots:
        """The one cap the worker's requests and the reviews share
        (A16). Each call opens its own handle; the lock files are what is
        shared, so the ingress and worker processes see the same pool."""
        return FileSlots(self._profile.paths.root / "slots", self._settings.max_concurrent)

    def plugin_job_runners(self, process: str) -> list[PeriodicRunner]:
        """Plugin background jobs for one process (G1). An engine-calling
        job waits for the same slots as requests and reviews, at most as
        long as one request may run."""
        return background_runners(
            self._hook_plugins(),
            process,
            self.concurrency_slots(),
            self._settings.request_timeout_sec,
        )

    def shadow_runners(self) -> list[PeriodicRunner]:
        """Shadow evaluation (G4): mirrors questions from each channel's
        `shadow_source` into it and answers them blind. Its engine turns wait
        for the same slots as requests, like a plugin job."""
        owner = self._owner_client

        def source_api(method: str, **params: Any) -> Any:
            return owner.api_call(method, params=params).data

        def post(channel: str, thread_ts: str | None, text: str) -> str | None:
            rich = channel_is_rich(self._channels.get(channel))
            return self.publisher().post(channel, thread_ts or "", text, rich)

        def judge_enabled(channel: str) -> bool:
            config = self._channels.get(channel)
            return bool(config and config.extra.get("shadow_judge"))

        service = ShadowService(
            store=ShadowStore(self._database),
            source_api=source_api if owner is not None else None,
            post=post,
            run_turn=self._shadow_turn,
            pairs=lambda: shadow_pairs(self._channels.all()),
            judge_enabled=judge_enabled,
            name_of=self.user_namer(),
            audit=self.audit().record,
            requests_path=self._profile.paths.root / "shadow_requests.json",
            bot_name=self._profile.display_name,
            is_self=lambda m: bool(m.get("user")) and m.get("user") == self.identity.user_id,
            max_parallel=self._settings.max_concurrent,
        )
        return background_runners(
            [ShadowPlugin(service)], "worker", self.concurrency_slots(), self._settings.request_timeout_sec,
        )

    def _shadow_turn(self, prompt: str, channel: str, request_id: str) -> TurnResult:
        """A read-only turn for someone who is not the owner: the question's
        author is a stranger in the source channel, and the answer must not
        reach what only the owner may see."""
        principal = Principal(user_id="", channel=channel, trust=TrustLevel.GENERAL, is_direct_message=False)
        config = self._channels.get(channel)
        system_prompt = self._composer().compose(CompositionContext(
            principal=principal,
            prompt=prompt,
            channel_mode=config.mode if config else "default",
            channel_slug=slug_for(channel, config),
            is_rich=channel_is_rich(config),
            chat_level=config.chat if config else "normal",
        ))
        tools = ToolSelection.from_names(self.tool_policy().tool_list_for(principal, prompt=prompt, readonly=True))
        response = self.engine_invoker.invoke(EngineRequest(
            prompt=prompt,
            system_prompt=system_prompt,
            session_id=None,
            resume=False,
            model=self.access_policy.model_for(principal),
            # Not the prompt: it carries the source question and thread replies,
            # and a third party's "ultrathink" would raise the effort.
            effort=self.access_policy.effort_for(principal, ""),
            workdir=self._profile.work_root,
            tools=tools,
            requirements=self.execution_policy.requirements_for(config=config, tools=tools),
            request_id=request_id,
            readable_dirs=self.readable_dirs,
            trust_level=TrustLevel.GENERAL,
        ), CallOrigin.BACKGROUND)
        # Shadow answers and judgements spend tokens with no request row of
        # their own; the per-model rollup reads this one instead (A14).
        record_engine_spend(self.audit().record, response, purpose="shadow_turn", channel=channel)
        calls = None
        if response.ok and response.session_id:
            reader: Any = self.transcript_reader(response.engine)
            find = getattr(reader, "find_transcript_path", None) or getattr(reader, "transcript_path", None)
            calls = tool_calls_in(find(response.session_id) if find else None)
        return TurnResult(ok=response.ok, body=response.body, tool_calls=calls)

    # --- 비소유자 DM 응대 (E1-E8) ----------------------------------------

    def dm_reply_service(self) -> DmReplyService | None:
        """None unless `dm_reply` is on and an owner is set: without an owner
        nobody would see the full copy of what a colleague was told (E5)."""
        if not self._settings.dm_reply or not self._profile.owner_user_id:
            return None
        if self._dm_reply is None:
            notice_log = UnregisteredNoticeLog(self._profile.paths.unregistered_channels)
            self._dm_reply = DmReplyService(
                run_turn=self._run_dm_turn,
                post=lambda channel, text: self.publisher().post(channel, "", text, False),
                audit=self.audit().record,
                notices=self._notices,
                daily_limit=self._settings.dm_daily_limit,
                notify_owner=self._notify_owner,
                slot=engine_slot(self.concurrency_slots(), self._settings.request_timeout_sec),
                context=lambda user: notice_log.recent_context(user, self._channels.is_registered),
                probe_warn_at=self._settings.dm_probe_warn_at,
            )
        return self._dm_reply

    def _dm_reply_callback(self) -> Callable[[str, str, str], None] | None:
        service = self.dm_reply_service()
        return service.handle if service is not None else None

    def _dm_attempt_logged(self) -> bool:
        """Refused DMs are recorded instead of answered: `dm_reply` off,
        `dm_attempt_log` on and an owner to report to (2026-11-11)."""
        return (
            not self._settings.dm_reply
            and self._settings.dm_attempt_log
            and bool(self._profile.owner_user_id)
        )

    def _dm_attempt_callback(self) -> Callable[[str, str, str], None] | None:
        """A refused DM with replies off: recorded when `dm_attempt_log` is on,
        answered with the profile's `dm_off_notice` when it has one. Before
        2026-11-13 the person got nothing back and took the bot for broken
        (11/12 조한솔님 "왜 답변이 지워지는거야")."""
        if self._settings.dm_reply:
            return None
        logged = self._dm_attempt_logged()
        notice = self._settings.dm_off_notice.strip()
        if not logged and not notice:
            return None
        record = self.audit().record if logged else None
        client = self._client

        def attempt(channel: str, user: str, text: str) -> None:
            notified = False
            if notice:
                try:
                    client.chat_postMessage(channel=channel, text=notice, unfurl_links=False)
                    notified = True
                except Exception:  # the attempt is still recorded
                    log.exception("DM 응대 꺼짐 안내를 보내지 못했다 : %s", user)
            if record is not None:
                record(
                    IncidentKind.DM_ATTEMPT, user=user, channel=channel, question=text,
                    notified=notified,
                )

        return attempt

    def dm_turn_tools(self, user: str, channel: str) -> ToolSelection:
        """The read-only selection a non-owner's DM turn gets: A10's lookup
        bundle and read-only MCP tools, no Skill, no browser tool, no bare
        write tool. Read, Grep and Glob are dropped as in mametchi (DM_TOOLS):
        the local files hold the owner's own records. Nothing left is a ban,
        never an open turn."""
        principal = Principal(
            user_id=user, channel=channel, trust=TrustLevel.GENERAL, is_direct_message=True,
        )
        policy = self.tool_policy()
        names = [
            name for name in policy.tool_list_for(principal, readonly=True)
            if name not in _DM_LOCAL_FILE_TOOLS and not _is_browser_tool(name)
        ]
        if not names:
            return ToolSelection.forbid_all()
        return ToolSelection.from_names(names, lookups=policy.lookups_for(principal, readonly=True))

    def dm_reply_system(self) -> str:
        """The DM guide plus the guards every non-owner turn carries
        (SensitiveGuardSection, NEVER_DISCLOSE). Appended after the guide, so
        a profile's dm_reply.md replaces the guide but cannot drop a guard; a
        missing guard file raises as in PromptComposer."""
        guide = load_prompt(
            self._profile.paths.prompts, REPLY_FILE, DEFAULT_REPLY_SYSTEM, self._profile.display_name,
        )
        library = self._prompt_library()
        return "\n\n".join((
            guide, library.text("SENSITIVE_GUARD").strip(), library.text("NEVER_DISCLOSE").strip(),
        ))

    def _dm_workdir(self) -> Path:
        # Empty on purpose: the turn has no business with any bot file.
        workdir = self._profile.paths.root / "dm-work"
        workdir.mkdir(parents=True, exist_ok=True)
        return workdir

    def _run_dm_turn(self, prompt: str) -> EngineResponse:
        # The DM channel is not needed by the turn; the tool list is the same
        # for every non-owner DM.
        tools = self.dm_turn_tools("", "D")
        return self.engine_invoker.invoke(EngineRequest(
            prompt=prompt,
            system_prompt=self.dm_reply_system(),
            # No session: every DM starts clean and leaves nothing to resume.
            session_id=None,
            resume=False,
            model=self._settings.dm_model or None,
            effort=DEFAULT_EFFORT,
            workdir=self._dm_workdir(),
            tools=tools,
            requirements=dm_requirements(tools),
            trust_level=TrustLevel.GENERAL,
            request_id=f"dm-{time.time():.6f}",
        ))

    def _run_dm_report_turn(self, prompt: str) -> EngineResponse:
        tools = ToolSelection.forbid_all()
        return self.engine_invoker.invoke(EngineRequest(
            prompt=prompt,
            system_prompt=load_prompt(
                self._profile.paths.prompts, REPORT_FILE, DEFAULT_REPORT_SYSTEM,
                self._profile.display_name,
            ),
            session_id=None,
            resume=False,
            model=self._settings.dm_model or None,
            effort=DEFAULT_EFFORT,
            workdir=self._dm_workdir(),
            tools=tools,
            requirements=self.execution_policy.requirements_for(config=None, tools=tools),
            # GENERAL: the prompt carries colleagues' DM text, untrusted like
            # every other Slack input a background turn reads.
            trust_level=TrustLevel.GENERAL,
            request_id=f"dm-report-{time.time():.6f}",
        ), CallOrigin.BACKGROUND)

    def dm_report_runners(self) -> list[PeriodicRunner]:
        """The daily DM report as a G1 background job on the worker, so its
        engine call waits for the same slots as requests. Empty unless DM
        replies are on, or attempts are recorded with replies off."""
        attempts_only = self._dm_attempt_logged()
        if self.dm_reply_service() is None and not attempts_only:
            return []
        report = DmDailyReport(
            rows=self.audit().rows_between,
            run_turn=self._run_dm_report_turn,
            notify_owner=self._notify_admin,
            audit=self.audit().record,
            notices=self._notices,
            state_path=self._profile.paths.root / "dm_daily_report.json",
            run_hour=self._settings.dm_report_hour,
            skip_empty=attempts_only,
        )
        job = BackgroundJob(
            name="dm_daily_report", run=report.tick,
            interval_sec=self._settings.dm_report_interval_sec,
        )
        return [background_job_runner(
            "core:dm_daily_report", job, self.concurrency_slots(), self._settings.request_timeout_sec,
        )]

    def worker(self, worker_id: str = "worker") -> Worker:
        # unlike the other components here, a new Worker is created on every
        # call — worker_id must differ per process
        worker = Worker(
            queue=self.queue(),
            handler=self.pipeline(),
            heartbeat=WorkerHeartbeat(self.queue(), self._settings),
            catchup=self._catchup(),
            markers=self.reactions(),
            settings=self._settings,
            worker_id=worker_id,
            inflight=self.inflight,
            active=self.active_inflight,
            # reclaim picks the mark from this (sca-o1e).
            watch_jobs=self.watch_jobs(),
            admin=self._admin_admission(),
            slots=self.concurrency_slots(),
        )
        # A marker from before this process started was for the restart
        # that produced it; following it again would loop.
        # Only an OWNER turn can be the requester the marker names; any other
        # turn that could write it must not get someone's request completed.
        # OWNER is the access policy's verdict. controller_user_ids are GENERAL
        # (A7): a delegated controller turn gets operator tools, not the
        # owner's, so it is not a restart requester either.
        policy = self.access_policy
        worker.watch_restart_requests(
            ApplyRequestWatch(
                paths=self._profile.paths.apply_requests, started_at=time.time()
            ).pending,
            requester_ok=lambda context: policy.principal_for(context.channel, context.user).trust
            is TrustLevel.OWNER,
        )
        return worker

    def _admin_router(self) -> AdminRouter:
        commands = default_admin_commands(self._notices)
        commands.extend(c for p in self._hook_plugins() for c in p.admin_commands())
        return AdminRouter(commands)

    def _hook_plugins(self) -> tuple[BotPlugin, ...]:
        """Profile plugins plus the core modules built on the same hooks:
        the welcome (G2). Kept out of `plugins`, which lists what the profile
        loaded."""
        if self._welcome is None:
            self._welcome = WelcomePlugin(self._channels, self.welcome_service)
        return (*self._plugins, self._welcome)

    def welcome_service(self) -> WelcomeService:
        """A read-only turn: it looks the joiner up and writes one post; the
        post itself goes through the publisher (G2). Owner tools cut to the
        read-only set, but the general model and effort: mametchi called it
        with is_owner=False, and any joiner starts one."""
        if self._welcome_service is None:
            owner = Principal(
                user_id=self._profile.owner_user_id, channel="",
                trust=TrustLevel.OWNER, is_direct_message=False,
            )
            caller = ReviewEngineCaller(
                invoker=self.engine_invoker,
                profile=self._profile,
                workdir=self._profile.work_root,
                system_prompt="슬랙 채널에 올릴 환영 글을 쓴다. 사용자 메시지의 지시를 따른다.",
                model=self._profile.primary_engine.model,
                effort=DEFAULT_EFFORT,
                tools=ToolSelection.allow(self.tool_policy().tool_list_for(owner, readonly=True)),
            )
            self._welcome_caller = caller
            library = PromptLibrary(self._profile.paths.prompts)
            self._welcome_service = WelcomeService(
                store=WelcomeStore(self._database),
                client=self._client,
                channels=self._channels,
                publisher=self.publisher(),
                run=lambda prompt: caller.run(prompt, None, False),
                template=lambda: library.text("welcome"),
                self_user_id=lambda: self.identity.user_id,
                audit=self.audit().record,
            )
        return self._welcome_service

    def _admin_admission(self) -> AdminAdmission:
        """Built once and shared: the socket path and catch-up have to reach
        the same decision, and two instances would drift (sca-oyku)."""
        if self._admin is None:
            self._admin = AdminAdmission(
                router=self._admin_router(),
                context_builder=self._admin_context,
                reply=self._reply,
                markers=self.reactions(),
                claims=self.admin_claims(),
                owner=self._claim_owner,
            )
        return self._admin

    def _admin_context(self, ctx: RequestContext) -> AdminContext:
        return AdminContext(
            principal=self.access_policy.principal_for(ctx.channel, ctx.user),
            channel=ctx.channel,
            thread_ts=ctx.thread_ts,
            channels=self._channels,
            profile=self._profile,
            text=ctx.text,
        )

    def _reply(self, channel: str, thread_ts: str, message: str) -> None:
        # admin command replies are always plain text, regardless of channel rich-mode config.
        # A flat_reply channel still gets them at top level, where its answers
        # land (B3, mametchi bot.py:6565).
        config = self._channels.get(channel)
        flat = bool(config and config.flat_reply)
        self.publisher().post(channel, thread_ts, message, rich=False, flat=flat)

    def _notify_peer_limit(self, event: Mapping[str, Any], limit: int) -> None:
        """Posts one line to the troubleshoot channel when a thread's
        bot-to-bot exchange first goes past the cap and this bot stops answering.

        2026-11-08 김태일 지시 : 핑퐁 한도에서 멈추는 것은 괜찮지만 아무도 모르게
        버려지면 안 된다. 어드민 채널(troubleshoot_channel)에 알린다.
        """
        target = self._profile.troubleshoot_channel
        if not target:
            return
        channel = str(event.get("channel") or "")
        ts = str(event.get("ts") or "")
        thread = str(event.get("thread_ts") or ts)
        link = ""
        try:
            link = str(self._client.chat_getPermalink(channel=channel, message_ts=ts).get("permalink") or "")
        except Exception:
            log.exception("한도 알림용 permalink 를 받지 못했다 : %s %s", channel, ts)
        sender = str(event.get("user") or event.get("bot_id") or "")
        who = f"<@{sender}>" if sender.startswith("U") else (sender or "다른 봇")
        text = (
            f"봇끼리 주고받기가 한도 {limit}턴을 넘어 {self._profile.display_name}가 <#{channel}> 스레드에서 "
            f"더 답하지 않고 멈췄습니다. 마지막 요청은 {who} 가 보냈습니다. "
            "사람이 스레드에 한 번 말하면 횟수가 다시 시작됩니다."
            + (f"\n{link}" if link else f"\nthread_ts={thread}")
        )
        self._client.chat_postMessage(channel=target, text=text, unfurl_links=False)

    def ingress(self) -> IngressService:
        if self._ingress is None:
            spawner = ThreadTaskSpawner()
            reactions = self.reactions()
            self._ingress = IngressService(
                listener=EventListener(
                    client=self._client,
                    channel_registry=self._channels,
                    gate=ResponseGate(),
                    identity=self.identity,
                    # Lets an unaddressed short approval ("좋아") land as an
                    # answer when the bot's prior turn marked the thread
                    # awaiting — including across a restart, or when the turn
                    # signalled only through the (now-stripped) [[ASK]] tag.
                    awaiting=SqliteAwaitingStore(self._database),
                    # Activates the bot-aware gates (C16/C17/C26): a message
                    # naming only other bots, or an unaddressed reply that
                    # follows a human's address to a peer, is left to that bot.
                    is_bot=self._bot_users.is_bot,
                    # Caps bot-to-bot roundtrips in a thread (C19), reset when
                    # a human speaks (C18), so two bots can't wake each other
                    # forever if the outgoing mention-strip ever misses.
                    peer_limiter=self._peer_exchange,
                    audit=self.audit().record,
                    # One Slack call off the handler thread, after the skip row.
                    mark_silent=lambda channel, ts: spawner.spawn(
                        f"넘김표식:{channel}:{ts}",
                        lambda: reactions.mark_skipped(channel, ts),
                    ),
                    peer_limit_notice=self._notify_peer_limit,
                ),
                # The one place the access check runs. Catch-up needs no second
                # one: it only sweeps channels that are already registered
                # (channel_ids()), which every user passes anyway (sca-a8pp).
                access=RequestAccess(
                    policy=self.access_policy,
                    channels=self._channels,
                    channel_name=self._channel_names(),
                    notices=self._notices,
                    reply=self._reply,
                    owner_user_id=self._profile.owner_user_id,
                    bot_name=self._profile.display_name,
                    self_user_id=lambda: self.identity.user_id,
                    notice_log=UnregisteredNoticeLog(self._profile.paths.unregistered_channels),
                    notify_owner=self._notify_owner if self._profile.owner_user_id else None,
                    permalink=SlackPermalinks(self._client).permalink,
                    audit=self.audit().record,
                ),
                strip_self_mention=SelfMentionStripper(self.identity).remove_self,
                dedup=DeduplicationTracker(),
                queue=self.queue(),
                reactions=reactions,
                attachments=self.attachments(),
                admin=self._admin_admission(),
                reply=self._reply,
                allowed_reactions=self.allowed_reactions(),
                on_reaction=self.on_reaction,
                spawn=spawner,
                notices=self._notices,
                is_shutting_down=lambda: self._shutting_down,
                extra_handlers=event_callbacks(
                    self._hook_plugins(),
                    ThreadTaskSpawner(),
                    self.concurrency_slots(),
                    self._settings.request_timeout_sec,
                    policy=self.access_policy,
                    self_user_id=lambda: self.identity.user_id,
                ),
                dm_reply=self._dm_reply_callback(),
                dm_attempt=self._dm_attempt_callback(),
                # The handler runs on one of slack-sdk's ten pool threads, so a
                # long lock wait here delays every event behind it (sca-9l1).
                lock_budget=lambda: self.database.latency_budget(
                    self._settings.ingress_lock_budget_sec
                ),
                # failed jobs get resurrected via re-registration; capping attempts
                # keeps a permanently-failing request from reviving on every redelivery
                job_max_attempts=self._settings.job_max_attempts,
                assistant=AssistantPanel(
                    client=self._client,
                    reply=self._reply,
                    greeting=self._profile.agent_greeting or DEFAULT_GREETING,
                    prompts=self._profile.agent_prompts,
                ),
            )
        return self._ingress

    def _download(self, url: str, token: str) -> DownloadResult:
        """Matches the signature AttachmentStore expects.

        Ignores the passed-in token — HttpDownloader fetches the same value
        itself from token_provider, and not threading it through as an
        argument is one less place it could leak into a log or exception.
        """
        return HttpDownloader(self._token_provider)(url)

    def allowed_reactions(self) -> frozenset[str]:
        return frozenset(self.review_tasks())

    def review_ledger(self) -> ReviewLedger:
        if self._review_ledger_instance is None:
            self._review_ledger_instance = ReviewLedger(
                self._database,
                # 점검은 엔진을 두 번까지 부른다(구분선 누락 재시도).
                # 그보다 짧게 잡으면 도는 점검을 중복 실행한다.
                stale_after_sec=self._settings.request_timeout_sec * 2,
            )
        return self._review_ledger_instance

    def stale_review_reporter(self) -> StaleReviewReporter:
        return StaleReviewReporter(
            self.review_ledger(),
            ReviewPublisher(self.publisher()),
            self._profile.troubleshoot_channel,
        )

    def stale_review_runner(self) -> PeriodicRunner:
        # 기동 시 1회로는 부족하다 — 점검이 중단되는 계기는 재기동만이 아니다.
        return PeriodicRunner(
            self.stale_review_reporter().sweep,
            self._settings.stale_review_sweep_interval_sec,
            name="stale_review",
        )

    def review_tasks(self) -> dict[str, ReviewTask]:
        if self._review_tasks is None:
            shared = {
                "ledger": self.review_ledger(),
                "message_lookup": SlackMessageLookup(self._client),
                "transcript": ThreadTranscriptPort(self._transcript_builder()),
                "answer_finder": AnswerRecordFinder(self._database),
                "reactions": self.reactions(),
                "permalinks": SlackPermalinks(self._client),
                "publisher": ReviewPublisher(self.publisher()),
                "troubleshoot_channel": self._profile.troubleshoot_channel,
                "owner_only_channels": self._settings.owner_only_channels,
                # 워커 요청과 같은 상한(A16). 권한·원장 판정을 지난 점검만
                # 자리를 기다린다. 거절된 리액션은 자리를 잡지 않는다.
                "slots": self.concurrency_slots(),
                "slot_wait_sec": self._settings.request_timeout_sec,
                # 점검은 몇 분이 걸린다. 표시가 없으면 도는 것과 죽은 것이
                # 사용자에게 같아 보인다(sca-tfd).
                "progress": ReviewProgressDisplay(self.progress(), self._channels),
                # 점검 한 건이 엔진을 몇 분씩 쓴다. 남기지 않으면 제한시간을
                # 어떻게 잡을지 정할 근거가 없다(sca-fy5).
                "audit": self.audit(),
                # 숫자만 남기면 느려진 것을 아무도 모른다. 요청 쪽과 같은
                # 보고자를 쓰되 기준값만 점검용으로 갈아 끼운다(sca-xck).
                "slow_reporter": self._slow_reporter(
                    self._settings.override(
                        {"slow_report_sec": self._settings.review_slow_report_sec}
                    )
                ),
            }
            paths = self._profile.paths
            owner_name = self._names.resolve(self._profile.owner_user_id)
            self._review_tasks = {
                POSTMORTEM_EMOJI: PostmortemTask(
                    bot_display_name=self._profile.display_name,
                    code_dir=str(_CODE_ROOT),
                    persona_dir=str(paths.persona),
                    engine=self._review_engine(postmortem=True),
                    **shared,
                ),
                DEBUG_TRACE_EMOJI: DebugTraceTask(
                    bot_display_name=self._profile.display_name,
                    code_dir=str(_CODE_ROOT),
                    persona_dir=str(paths.persona),
                    owner_display_name=owner_name,
                    engine=self._review_engine(debug_trace=True),
                    **shared,
                ),
                FORMAT_REVIEW_EMOJI: FormatReviewTask(
                    bot_display_name=self._profile.display_name,
                    persona_dir=str(paths.persona),
                    prompts_dir=str(paths.prompts),
                    rewrite_command=self.rewrite_command(),
                    rewriter=self.publisher(),
                    engine=self._review_engine(format_review=True),
                    **shared,
                ),
            }
        return self._review_tasks

    def rewrite_command(self) -> str:
        """The command the format review runs to correct the message it flagged.

        Assembled here because the interpreter and the profile are deployment
        facts the review package has no way to know. `sys.executable` rather
        than "python3" — the engine subprocess inherits a PATH that need not
        have this venv on it, and a different interpreter wouldn't have the
        package installed. `--profile-dir` is pinned to the directory this
        profile was actually loaded from; the default search order depends on
        an env var the subprocess may not carry.
        """
        parts = [
            shlex.quote(sys.executable), "-m", "slack_cli_agent.cli", "rewrite",
            "--profile", shlex.quote(self._profile.name),
        ]
        source = self._profile.source_file
        if source is not None:
            parts += ["--profile-dir", shlex.quote(str(source.parent))]
        return " ".join(parts)

    def _review_engine(
        self, *, postmortem: bool = False, debug_trace: bool = False, format_review: bool = False
    ) -> ReviewEngineCaller:
        """One caller per review kind: each carries its own *_NOTE.

        The review prompts say "follow the format in the system prompt", so an
        empty system prompt left the engine with no format, no "do not modify"
        rule and no persona. The original bot.py built these turns through
        build_system_prompt with postmortem/debug_trace/format_review set, on
        the troubleshoot channel as the owner (bot.py _run_postmortem,
        _run_debug_trace, _run_format_review).
        """
        paths = self._profile.paths
        channel = self._profile.troubleshoot_channel
        principal = Principal(
            user_id=self._profile.owner_user_id,
            channel=channel,
            trust=TrustLevel.OWNER,
            is_direct_message=is_direct_message_channel(channel),
        )
        # ReviewEngineCaller passes no tools, so a review runs unrestricted as
        # the owner; the tier says that rather than readonly, which mametchi
        # uses because its review strips tools (A12 review).
        # Worked out per call like model below: review_tasks() is cached, and
        # a value fixed here would miss a channels.json edit until a restart.
        review_tools = ToolSelection.unrestricted()

        # A non-owner's review (A18) runs with that person's read-only tools,
        # so its tier and reach are worked out from the requester and the
        # tools the turn actually got, not from the owner's.
        def tier(
            reviewer: Principal | None = None, tools: ToolSelection = review_tools,
        ) -> str:
            who = reviewer or principal
            return turn_tier(
                who, self._channels.get(channel),
                full_authority=self.access_policy.is_full_authority(who),
                readonly=not tools.can_act,
            ).value

        def system_prompt(
            prompt: str, reviewer: Principal = principal, tools: ToolSelection | None = None,
        ) -> str:
            # The owner turn's reach note names the tools the adapter will hand
            # it: a format review gets owner_format_tools, not unrestricted.
            if tools is None:
                tools = owner_format_tools(prompt) if format_review else review_tools
            config = self._channels.get(channel)
            reads_only, acting = turn_reach(self.engine, EngineRequest(
                prompt=prompt, system_prompt="", session_id=None, resume=False,
                model=None, effort="", workdir=self._profile.work_root,
                tools=tools, trust_level=reviewer.trust,
            ))
            return self._composer().compose(CompositionContext(
                principal=reviewer,
                # The review prompt carries the transcript; its words pick the
                # topic knowledge, as bot.py load_persona did with the prompt.
                prompt=prompt,
                channel_mode=config.mode if config else "default",
                channel_slug=slug_for(channel, config),
                # channel is left empty on purpose: a review turn has no thread
                # of its own, so ThreadCoordsSection would print an empty
                # thread_ts and an upload would land at the channel top level.
                # bot.py added thread_coords only on the normal reply path.
                is_rich=channel_is_rich(config),
                chat_level=config.chat if config else "normal",
                authority_granted=self.access_policy.is_full_authority(reviewer),
                # Same rule as an owner turn on the normal path (pipeline.py
                # _compose_system_prompt), so a review sees the domain facts
                # the reviewed conversation saw.
                include_domain=_domain_applies(
                    config.workdir if config else None, Path.home()
                ),
                postmortem=postmortem,
                debug_trace=debug_trace,
                format_review=format_review,
                turn_tier=tier(reviewer, tools),
                turn_reads_only=reads_only,
                turn_acting_tools=acting,
            ))

        def scoped(requester: Principal, prompt: str) -> tuple[ToolSelection, str]:
            """A non-owner's review turn (A18). Every kind gets this person's
            read-only tools, on Claude and Codex alike. A format review's
            correction comes back in the reply and FormatReviewTask writes it
            to the flagged message itself: a Bash(<rewrite>:*) grant left
            --channel, --update and the file argument open, and the turn had
            no Write to make the file anyway (A18 review). The system prompt
            is composed for this person, so the owner's guidance does not
            reach their turn."""
            policy = self.tool_policy()
            names = list(policy.tool_list_for(requester, prompt=prompt, readonly=True))
            tools = ToolSelection.from_names(
                names, lookups=policy.lookups_for(requester, readonly=True),
            )
            reviewer = replace(
                requester, channel=channel, is_direct_message=is_direct_message_channel(channel),
            )
            return tools, system_prompt(prompt, reviewer, tools)

        def owner_format_tools(prompt: str) -> ToolSelection:
            """The owner's format review turn (B6).

            Unrestricted names no tools to claude, and dontAsk then refused
            Write, the rewrite Bash and every Slack lookup, so the correction
            the prompt asks for never ran (#test-agent 1794048787.569739). The
            owner's read-only tools plus what the fix itself needs: Write for
            the correction file and the rewrite command, not a bare Bash. Each
            is added only when the owner's own turn already has the bare tool,
            so a profile that keeps Write or Bash from its owner does not get
            them back through a reaction."""
            policy = self.tool_policy()
            owner_turn = policy.tool_list_for(principal, prompt=prompt)
            names = list(policy.tool_list_for(principal, prompt=prompt, readonly=True))
            for needs, grant in (
                ("Write", "Write"), ("Bash", f"Bash({self.rewrite_command()}:*)"),
            ):
                if needs in owner_turn and grant not in names:
                    names.append(grant)
            return ToolSelection.from_names(
                names, lookups=policy.lookups_for(principal, readonly=True),
            )

        return ReviewEngineCaller(
            # passing the runner directly here would also skip fallback-switch handling
            invoker=self.engine_invoker,
            profile=self._profile,
            workdir=self._profile.work_root,
            system_prompt=system_prompt,
            # 2026-11-07 김태일 "권한막힌게 왜이렇게 많니". 부검 지시는 코드를 열어 보라는데
            # 코드 위치를 work_root 로 알려 주고 실제 코드 폴더는 읽기 경로에 없어 Read 가 거부됐다.
            scoped=scoped,
            owner_tools=owner_format_tools if format_review else None,
            readable_dirs=(paths.persona, paths.prompts, _CODE_ROOT),
            tools=review_tools,
            turn_tier=tier,
            # 부검만 전용 모델이다 (마메치 model_for postmortem=True). 디버그
            # 추적과 서식 점검은 보고 채널의 일반 규칙(어드바이저 실행자, 채널
            # model, 기본 model)을 따른다. 마메치 run_claude 가 이 둘을
            # model_for(TROUBLESHOOT_CHANNEL, is_owner) 로 고르는 것과 같다.
            # 호출할 때마다 고른다. review_tasks() 가 캐시되므로 값으로 넘기면
            # 보고 채널 model 이나 advisor.executor 수정이 재기동 전까지
            # 점검에만 반영되지 않는다.
            model=(
                self._profile.primary_engine.model_for_postmortem if postmortem
                else lambda: self.access_policy.model_for(principal)
            ),
        )

    def on_reaction(self, emoji: str, channel: str, ts: str, by_user: str) -> None:
        """Catches and logs review-task failures instead of propagating —
        otherwise one bad review event would block all future ones, and a
        swallowed failure would look identical to one that was never triggered.
        """
        task = self.review_tasks().get(emoji)
        if task is None:
            return
        # The original bot.py had no permission check here (bot.py:6144), but a
        # review runs at medium effort or higher (postmortem on the postmortem
        # model, trace and format review on the report channel's model_for), so
        # anyone adding the emoji repeatedly spends that. Owner and the channel's
        # trusted_users only (사용자 결정 2026-09-18, sca-cg9).
        # `accepts_request` on top of that: trust alone let a DM entry in the
        # channel file hand a non-owner a conversation a normal request would
        # have been refused in (sca-gr5n).
        principal = self.access_policy.principal_for(channel, by_user)
        if principal.trust is TrustLevel.GENERAL or not self.access_policy.accepts_request(
            principal
        ):
            log.info("점검 권한이 없다 : %s %s:%s %s", emoji, channel, ts, by_user)
            return
        config = self._channels.get(channel)
        try:
            task.run(
                ReviewTarget(
                    channel=channel,
                    ts=ts,
                    by_user=by_user,
                    channel_name=getattr(config, "name", "") or channel,
                    rich=channel_is_rich(config),
                    # The review runs at this person's level, not the owner's (A18).
                    requester=principal,
                )
            )
        except Exception:
            log.exception("점검 실패: %s %s:%s", emoji, channel, ts)

    def transcript_reader(self, engine: str = "") -> SessionTranscriptReader:
        """Transcript format and home dir differ per engine, so the reader is
        chosen by the engine that actually answered — a fallback response comes
        from the secondary, and reading it with the primary's reader would look
        for the wrong format in the wrong place. Cached per engine because both
        the time-breakdown and usage-row calculations read the same file.
        """
        spec = self._engine_spec_for(engine)
        name = spec.type if spec is not None else engine
        reader = self._transcript_reader_cache.get(name)
        if reader is None:
            reader = self._transcript_readers.create(name, self._profile.work_root, spec=spec)
            self._transcript_reader_cache[name] = reader
        return reader

    def _codex_home_for(self, engine: str) -> Path | None:
        """CODEX_HOME of the slot that answered, None for a non-codex engine.

        A spec without home_dir runs codex under its own default, ~/.codex.
        """
        spec = self._engine_spec_for(engine)
        if spec is None or spec.type != "codex":
            return None
        return spec.home_dir if spec.home_dir is not None else Path.home() / ".codex"

    def _engine_spec_for(self, engine: str) -> EngineSpec | None:
        """None for a name in neither slot. The registry then hands back an empty
        reader, so the report says nothing rather than reading the primary's
        transcript and presenting another engine's numbers as this one's.
        """
        if not engine or engine == self._profile.primary_engine.type:
            return self._profile.primary_engine
        fallback = self._profile.fallback_engine
        if fallback is not None and engine == fallback.type:
            return fallback
        return None

    def _slow_reporter(self, settings: RuntimeSettings | None = None) -> SlowRequestReporter:
        # an empty troubleshoot_channel is valid config — the reporter just
        # skips past the threshold check in that case
        return SlowRequestReporter(
            publisher=self.publisher(),
            # Resolved per report, not bound here — see transcript_reader().
            readers=self.transcript_reader,
            calculator=TimeBreakdownCalculator(self._settings.assumed_tokens_per_sec),
            # built explicitly — the default has no session-context calculator,
            # so the usage row's "session" line would never appear
            usage_row_builder=UsageRowBuilder(
                self._settings.owner_only_channels,
                SessionContextCalculator(self._settings.context_limit),
            ),
            diagnostician=ElapsedDiagnostician(self._settings.sleep_gap_suspect_sec),
            formatter=SlowReportFormatter(self._settings.assumed_tokens_per_sec),
            settings=settings or self._settings,
            troubleshoot_channel=self._profile.troubleshoot_channel,
        )

    def roster_builder(self) -> RosterBuilder:
        """Not built in __init__ — that would make assembly itself require a
        Slack call, tying pre-startup checks to the network. Cached so the
        periodic refresher and direct calls write through the same instance.
        """
        if self._roster_builder is None:
            self._roster_builder = RosterBuilder(
                self._client,
                self._profile.roster_file,
                handle_pattern=self._settings.roster_handle_pattern,
            )
        return self._roster_builder

    def roster_refresher(self) -> PeriodicRunner:
        """Doesn't start itself. Ingress and worker processes assemble the
        same Application, and if both refreshed the roster they'd write the
        same file concurrently — which process owns it is that process's
        call, not assembly's. Cached so the instance that gets started is
        the one that gets stopped, or stop() would leave an earlier thread running.
        """
        if self._roster_refresher is None:
            self._roster_refresher = PeriodicRunner(
                self.roster_builder().refresh,
                self._settings.roster_refresh_sec,
                name="roster",
            )
        return self._roster_refresher

    def owner_only_audit_runner(self) -> PeriodicRunner:
        """소유자 전용 선언과 실제 멤버가 어긋나는지 본다. ingress 에만 둔다 -
        워커는 여러 개가 돌 수 있어 같은 경고가 여러 번 나간다."""
        audit = OwnerOnlyChannelAudit(
            channels=sorted(self._settings.owner_only_channels),
            owner_user_id=self._profile.owner_user_id,
            # controller 는 소유자와 같은 최고 권한을 받으므로 소유자 전용 채널의
            # 정당한 멤버다. 외부인(유출 대상)으로 오탐하지 않게 함께 넘긴다.
            controller_user_ids=self._profile.controller_user_ids,
            list_members=self._channel_members,
            # 슬랙 멤버 목록은 봇도 사용자 ID 로 준다. 사람인지는 users.info 의
            # is_bot 으로만 갈린다.
            is_bot=self._is_bot_user,
            notify=self._notify_owner if self._profile.owner_user_id else None,
        )
        return PeriodicRunner(
            audit.check,
            self._settings.owner_only_audit_interval_sec,
            name="owner_only_audit",
        )

    def usage_check_runner(self) -> PeriodicRunner:
        """운영자가 건 사용량 확인 명령을 주기로 돌린다. ingress 에만 둔다 -
        워커마다 돌면 같은 알림이 여러 번 나간다."""
        check = UsageCheck(
            self._profile.usage_check_command,
            self._usage_check_run,
            timeout_sec=self._settings.usage_check_timeout_sec,
        )
        return PeriodicRunner(
            check.check_once,
            self._settings.usage_check_interval_sec,
            name="usage_check",
            initial_delay_sec=USAGE_CHECK_FIRST_DELAY_SEC,
        )

    def _channel_members(self, channel: str) -> list[str]:
        members: list[str] = []
        cursor = ""
        while True:
            response = self.client.conversations_members(
                channel=channel, limit=200, cursor=cursor or None
            )
            members.extend(response.get("members", []))
            cursor = (response.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                return members

    def _is_bot_user(self, user_id: str) -> bool:
        # 조회가 안 되면 봇으로 본다. 사람으로 보면 조회 실패가 곧 위반 경고가
        # 되어, 고칠 것이 없는데 매 주기 알림이 나간다.
        try:
            user = self.client.users_info(user=user_id).get("user") or {}
        except Exception as exc:  # noqa: BLE001 — 한 사람의 조회 실패가 점검을 끊으면 안 된다
            log.warning("사용자 조회 실패, 봇으로 본다 : %s : %s", user_id, exc)
            return True
        return bool(user.get("is_bot") or user.get("id") == "USLACKBOT")

    def connection_watch(self) -> SocketErrorWatch:
        """SocketErrorWatch is a logging.Handler; attaching it to loggers is
        assembly's job, not the watch's own — without this, socket drops go
        uncounted, and a dormant watch looks identical to no watch at all.

        Attaches to both slack_sdk and slack_bolt loggers since SlackGateway
        takes an injected connector and could be wired to either.
        """
        if self._connection_watch is None:
            watch = SocketErrorWatch()
            for name in SOCKET_LOGGERS:
                logging.getLogger(name).addHandler(watch)
            self._connection_watch = watch
        return self._connection_watch

    def health_monitor(self, restart: Callable[[str], None]) -> HealthMonitor:
        # restart isn't performed here — killing the process is a real side
        # effect, so only the reason is handed to the restart callback
        return HealthMonitor(
            watch=self.connection_watch(),
            reachable=self._slack_reachable,
            restart=restart,
            settings=self._settings,
        )

    def health_runner(self, restart: Callable[[str], None]) -> PeriodicRunner:
        """Without this runner, socket errors could pile up forever without
        ever triggering a restart. The monitor is cached, not rebuilt per
        tick, because it tracks its down-since timestamp internally —
        recreating it would make a recovered connection look like it never went down.
        """
        if self._health_runner is None:
            monitor = self.health_monitor(restart)
            self._health_runner = PeriodicRunner(
                monitor.check,
                self._settings.health_interval_sec,
                name="health",
            )
        return self._health_runner

    def attachments(self) -> AttachmentStore:
        # cached so ingress and the cleanup runner share the same store —
        # otherwise they could point at different directories, and an
        # anonymous instance would give cleanup nothing to reference
        if self._attachments is None:
            self._attachments = AttachmentStore(
                attach_dir=self._profile.attach_dir,
                token_provider=self._token_provider,
                downloader=self._download,
            )
        return self._attachments

    def attachment_cleanup_runner(self) -> PeriodicRunner:
        # without this, downloaded attachments accumulate forever
        return PeriodicRunner(
            self._cleanup_attachments,
            self._settings.attachment_cleanup_interval_sec,
            name="attachment_cleanup",
        )

    def _cleanup_attachments(self) -> None:
        self._attachment_log.record(self.attachments().cleanup())

    def catchup_retry_runner(self, worker: Worker) -> PeriodicRunner:
        # Slack returning an empty channel history is usually transient —
        # without a retry, requests from that window never get picked up by any path
        return PeriodicRunner(
            lambda: self._catchup_retry_tick(worker),
            self._settings.catchup_retry_interval_sec,
            name="catchup_retry",
        )

    def connection_epochs(self) -> SqliteConnectionEpochs:
        """One ledger per process: the liveness-write interval is instance
        state, so extra instances would multiply the writes."""
        if self._connection_epochs is None:
            # Wall clock on purpose: this ledger is read by another process,
            # and self._clock is monotonic, whose origin differs per process.
            self._connection_epochs = SqliteConnectionEpochs(self._database)
        return self._connection_epochs

    def connection_catchup_coordinator(self, worker: Worker) -> ConnectionCatchupCoordinator:
        return ConnectionCatchupCoordinator(
            store=self.connection_epochs(),
            catch_up=lambda window: self._connection_catchup(worker, window),
            settings=self._settings,
            owner=worker.worker_id,
        )

    def connection_catchup_runner(self, worker: Worker) -> PeriodicRunner:
        """Makes a socket reconnect trigger catch-up.

        The OutageTracker behind catchup_retry_runner only sees Web API
        reachability from this process, which stays fine while the ingress
        socket is down (measured 2026-09-16).
        """
        return PeriodicRunner(
            self.connection_catchup_coordinator(worker).tick,
            self._settings.connection_catchup_interval_sec,
            name="connection_catchup",
        )

    def _connection_catchup(self, worker: Worker, window: float) -> None:
        report = worker.catch_up(self.channel_ids(), window_sec=window)
        log.info("소켓 재연결 캐치업: 다시 처리한 요청 %d건", len(report.missed))

    def startup_catchup_runner(self, worker: Worker) -> PeriodicRunner:
        """Sweeps once at start and once more after the freshness grace period.

        The interval is that grace plus a margin, so the second pass sees the
        mentions the first one skipped for being too recent.
        """
        catchup = StartupCatchup(lambda: self._startup_catchup_tick(worker))
        return PeriodicRunner(
            catchup.tick,
            self._settings.catchup_grace_sec + 5,
            name="startup_catchup",
        )

    def channel_join_runner(self) -> PeriodicRunner:
        """Joins the `"join": true` channels once at start (G3). Pinned to
        the ingress, which runs once, so several workers don't each call
        conversations.join. The interval only spaces the one pass from the
        no-op ticks after it."""
        once = StartupCatchup(
            lambda: join_registered_channels(self._client, self._channels, audit=self.audit().record),
            passes=1,
        )
        return PeriodicRunner(once.tick, 3600.0, name="channel_join")

    def _startup_catchup_tick(self, worker: Worker) -> None:
        report = worker.catch_up(self.channel_ids())
        log.info("기동 캐치업: 다시 처리한 요청 %d건", len(report.missed))

    def outage_tracker(self) -> OutageTracker:
        # cached — recreating it each tick would lose the previous result
        # needed to detect a recovery
        if self._outage_tracker is None:
            self._outage_tracker = OutageTracker(reachable=self._slack_reachable, now=self._clock)
        return self._outage_tracker

    def _recovery_window_sec(self, outage_sec: float) -> float:
        """Widens the catch-up window to cover the outage, plus a 600s
        margin for detection lag. Capped so one pass can't end up
        re-reading days of channel history.
        """
        return min(
            max(self._settings.catchup_window_sec, outage_sec + 600),
            self._settings.catchup_max_window_sec,
        )

    def _report_recovery(
        self,
        outage_sec: float,
        recovered: int,
        *,
        catchup_failed: bool = False,
        network_outage_sec: float | None = None,
    ) -> None:
        # reports the recovered count explicitly — otherwise zero recovered
        # messages would look the same as catch-up never having run
        broken = broken_mcp_servers(self._profile.mcp_servers)
        lines = [
            "*연결 복구*",
            "",
            f"- 끊긴 시간 : {outage_sec / 60:.0f}분",
            f"- 복구 시각 : {datetime.now(KST).strftime('%m-%d %H:%M:%S')} KST",
        ]
        if catchup_failed:
            # 0건과 "캐치업 자체가 실패" 는 다른 상황이다. 전자로 보이면
            # 운영자가 그 구간이 회수됐다고 오해한다.
            lines.append("- 캐치업 실패 : 끊긴 구간의 요청을 회수하지 못했습니다")
        else:
            lines.append(f"- 캐치업으로 처리한 요청 : {recovered}건")
        lines.append(
            f"- MCP 서버 : {'전부 정상' if not broken else f'{len(broken)}개 이상 기동 실패'}"
        )
        if broken:
            lines += ["", "*기동하지 못하는 MCP 서버*"]
            lines += [f"- {name}" for name in broken]
            lines += ["", "도구가 빠진 채로 답하게 됩니다. 확인이 필요합니다."]
        if network_outage_sec is not None:
            # 소켓 아래 계층(DNS/Wi-Fi)까지 닿지 않은 지속 장애는 슬랙 API 블립과
            # 다른 사고다. 2026-11-02 의 Errno 49 처럼 프로세스는 살아 있는데
            # 이름해석·경로가 끊긴 상황을 운영자가 구분할 수 있게 그 사실과 기간을
            # 따로 싣는다 (G59). assemble_recovery_report 가 그 줄을 조립한다.
            lines += [
                "",
                assemble_recovery_report(
                    reason="소켓 아래 계층(DNS/Wi-Fi)까지 닿지 않는 네트워크 수준 장애로 보입니다. "
                    "이름해석·경로를 확인해 주세요.",
                    now=self._clock(),
                    network_outage_sec=network_outage_sec,
                ),
            ]
        self._notify_owner("\n".join(lines))

    def _catchup_retry_tick(self, worker: Worker) -> None:
        outage_sec = self.outage_tracker().check()
        if outage_sec is not None:
            # A report left unsent during a restart is most often the restart
            # announcement itself, which failed because the socket was unstable
            # -- exactly the instability this recovery resolves. Flush it now,
            # in the same tick, instead of leaving it for the independent
            # pending_report runner (up to its interval later). Matches
            # bot.py:11147-11148, where the health loop flushes before catch-up
            # (the fix for a 2026-08-26 report that sat unsent for 139 minutes).
            self.pending_report().flush()
            # events during the outage never re-arrive via the socket, so we
            # must catch up now or lose them permanently
            log.info("슬랙 연결이 돌아왔다. 끊긴 시간 %.0f초", outage_sec)
            # check() already flipped the tracker back to healthy, so this
            # recovery can never be re-detected. The owner notification must
            # go out even if catch-up throws — otherwise a catch_up exception
            # swallows the only signal that the outage happened at all.
            # A Slack-API outage that outlasts capture_after_sec is, by the
            # 2026-11-02 incident's logic, long past a transient API blip and
            # most likely network-level (DNS/Wi-Fi). Flag it so the recovery
            # report names that layer; a short blip stays a plain reconnect.
            network_outage_sec = (
                outage_sec
                if self._network_escalation.needs_capture(
                    elapsed_sec=outage_sec, already_captured=False
                )
                else None
            )
            try:
                report = worker.catch_up(
                    self.channel_ids(), window_sec=self._recovery_window_sec(outage_sec)
                )
            except Exception:
                log.exception("복구 캐치업 실패")
                self._report_recovery(
                    outage_sec, 0, catchup_failed=True, network_outage_sec=network_outage_sec
                )
            else:
                self._report_recovery(
                    outage_sec, len(report.missed), network_outage_sec=network_outage_sec
                )

        for status in worker.retry_catchup():
            if not status.alert:
                continue
            # silently retrying forever would hide a channel stuck for hours from anyone
            self._notify_owner(
                "*캐치업을 오래 마치지 못하고 있습니다*\n\n"
                f"- 채널 : {status.channel}\n"
                f"- {status.stuck_sec / 60:.0f}분째입니다\n\n"
                f"{_stuck_cause_text(status)}"
            )

    def ingress_services(self, restart: Callable[[str], None]) -> ServiceGroup:
        """Health check only runs here since the socket connection lives only
        in this process. Roster refresh is also pinned to ingress — multiple
        workers can run at once, and refreshing from there would mean
        several processes writing the same file concurrently.
        """
        return ServiceGroup(
            [
                self.health_runner(restart),
                self.roster_refresher(),
                self.owner_only_audit_runner(),
                self.usage_check_runner(),
                self.attachment_cleanup_runner(),
                self.pending_report_runner(),
                self.stale_review_runner(),
                self.channel_join_runner(),
                *self.plugin_job_runners("ingress"),
            ],
            name="ingress",
            watch_interval_sec=self._settings.service_watch_interval_sec,
            notify=self._notify_owner if self._profile.owner_user_id else None,
        )

    def worker_services(self, worker: Worker) -> ServiceGroup:
        # catchup retry enqueues onto this specific worker's queue, hence the worker argument
        return ServiceGroup(
            [
                self.state_snapshot_runner(),
                self.watch_runner(hold=worker.holding_claims),
                self.watch_result_cleanup_runner(),
                self.job_purge_runner(),
                self.epoch_purge_runner(),
                self.admin_claim_purge_runner(),
                self.admin_claim_reclaim_runner(),
                self.startup_catchup_runner(worker),
                self.connection_catchup_runner(worker),
                self.catchup_retry_runner(worker),
                self.pending_report_runner(),
                self.learning_batch_runner(),
                self.stale_reclaim_runner(worker),
                *self.shadow_runners(),
                *self.plugin_job_runners("worker"),
                *self.dm_report_runners(),
            ],
            name="worker",
            watch_interval_sec=self._settings.service_watch_interval_sec,
            notify=self._notify_owner if self._profile.owner_user_id else None,
        )

    def self_restarter(self, exit_process: Callable[[int], None] | None = None) -> SelfRestarter:
        # only notifies when an owner is configured — otherwise there's
        # nowhere to send it, so the reason just goes to the log
        return SelfRestarter(
            notify=self._notify_owner if self._profile.owner_user_id else None,
            inflight_count=lambda: self.inflight.count,
            grace_sec=self._settings.shutdown_grace_sec,
            exit_process=exit_process,
            on_shutdown_start=self.mark_shutting_down,
        )

    def _slack_reachable(self) -> bool:
        try:
            self._client.auth_test()
            return True
        except Exception:  # noqa: BLE001 — any exception here means unreachable, by design
            return False

    @property
    def identity(self) -> BotIdentity:
        # shared by every component that needs is-self-message checks —
        # building it per-component would multiply the API calls and let one
        # failure produce an inconsistent verdict
        if self._identity is None:
            self._identity = SlackBotIdentity(
                self._client,
                clock=self._clock,
                retry_interval_sec=self._settings.identity_retry_interval_sec,
            )
        return self._identity

    def _is_self_message(self, msg: Any) -> bool:
        return self.identity.is_self(msg)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._roster_refresher is not None:
            # without stopping it, the daemon thread keeps calling Slack until process exit
            self._roster_refresher.stop()
        if self._connection_watch is not None:
            # without removing it, handlers pile up on the logger across
            # process restarts and the same log line gets double-counted
            for name in SOCKET_LOGGERS:
                logging.getLogger(name).removeHandler(self._connection_watch)
        self._database.close()


def _watch_workdir(job: WatchJob, config: ChannelConfig | None, work_root: Path) -> Path:
    """Where the check turn runs.

    The registration-time directory wins: the work being watched left its
    result there, and the channel config may have moved since. Rows written
    before that was recorded have none, so those fall back (sca-6zt).
    """
    if job.workdir:
        return Path(job.workdir)
    if config and config.workdir:
        return config.workdir.resolve()
    return work_root.resolve()
