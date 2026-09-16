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
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from ..admin.command import AdminContext
from ..admin.defaults import default_admin_commands
from ..admin.router import AdminRouter
from ..auth.policy import AccessPolicy
from ..auth.principal import Principal, TrustLevel
from ..auth.tools import ToolPolicy
from ..config.channel import ChannelConfig, ChannelRegistry
from ..config.profile import EngineSpec, Profile
from ..config.settings import RuntimeSettings
from ..engine.base import CallOrigin, Engine, EngineRequest, EngineResponse
from ..engine.claude import ClaudeEngine
from ..engine.codex import CodexEngine
from ..engine.gemini import GeminiEngine
from ..engine.registry import EngineRegistry
from ..engine.runner import (
    DirectInvoker,
    EngineInvoker,
    EngineRunner,
    FallbackEngine,
    FallbackInvoker,
)
from ..engine.switcher import EngineSwitcher
from ..engine.transcript import (
    SessionTranscriptReader,
    TranscriptReaderRegistry,
)
from ..guard.base import OutputGuard
from ..guard.dropline import ConfiguredLineDropGuard
from ..guard.mentions import AddresseeGuard, PlainMentionGuard
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
from ..observability.audit import AuditLog
from ..observability.notices import NoticeCatalog
from ..observability.progress import ProgressCoordinator
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
from ..plugin.loader import PluginLoader
from ..prompt.composer import SystemPromptComposer
from ..prompt.knowledge import KnowledgeLoader
from ..prompt.library import PromptLibrary
from ..prompt.linked_threads import LinkedThreadNote
from ..prompt.sections import (
    AskerSection,
    AuthoritySection,
    ChannelModeSection,
    CompositionContext,
    KnowledgeSection,
    OwnerNoteSection,
    PersonaSection,
    PresentPeopleSection,
    PromptSection,
    ReviewFormatSection,
    RosterSection,
    SensitiveGuardSection,
    SilenceRuleSection,
    SlackFormatSection,
    TrustedSection,
    WatchSection,
)
from ..reliability.catchup import CatchupService
from ..reliability.connection import ConnectionCatchupCoordinator
from ..reliability.dedup import DeduplicationTracker
from ..reliability.health import HealthMonitor, SelfRestarter, SocketErrorWatch
from ..reliability.outage import OutageTracker
from ..reliability.pending_report import PendingReportStore
from ..reliability.startup import StartupCatchup
from ..reliability.watchjobs import WatchJob, WatchJobQueue
from ..reliability.watchresult import WatchOutcome, WatchResultReader
from ..reliability.watchrunner import WatchJobChecker, watch_check_prompt
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
from ..session.manager import SessionManager
from ..session.store import SqliteSessionStore
from ..slack.attachments import AttachmentStore, DownloadResult
from ..slack.credentials import CredentialResolver, resolver_for
from ..slack.download import HttpDownloader
from ..slack.gate import ResponseGate
from ..slack.gateway import SlackGateway
from ..slack.history import HistoryReader
from ..slack.history_port import SlackHistoryPort
from ..slack.identity import BotIdentity, SlackBotIdentity
from ..slack.late_addendum import LateAddendumChecker, ThreadConsumption
from ..slack.linked_threads import LinkedThreadReader
from ..slack.listener import EventListener
from ..slack.names import DisplayNameResolver
from ..slack.participants import ThreadParticipants
from ..slack.progress import (
    FallbackProgressSink,
    SlackProgressSink,
    SlackStreamingProgressSink,
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
from ..slack.transcript import TranscriptBuilder
from ..storage.connection_epochs import SqliteConnectionEpochs
from ..storage.database import Database
from .channel_kind import is_direct_message_channel
from .context import RequestContext
from .errors import AgentError
from .ingress import IngressService
from .lifecycle import InflightCounter
from .periodic import PeriodicRunner
from .pipeline import RequestPipeline
from .services import ServiceGroup
from .spawn import ThreadTaskSpawner
from .timezones import KST
from .worker import Worker

log = logging.getLogger(__name__)

# One prompt file per channel mode, by naming convention — mode "agent_coach"
# maps to prompts/prompt_agent_coach.md without needing a lookup table.
DEFAULT_PROMPT = "PROMPT_DEFAULT"
KNOWN_MODES: tuple[str, ...] = ("private", "agent_coach")

# Loggers whose socket-connection warnings the connection watch attaches to.
SOCKET_LOGGERS: tuple[str, ...] = ("slack_sdk.socket_mode", "slack_bolt")


def mode_prompt_names(modes: Sequence[str] = KNOWN_MODES) -> dict[str, str]:
    return {mode: f"PROMPT_{mode.upper()}" for mode in modes}


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
    ) -> None:
        self._profile = profile
        self._client = client
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

        self._channels = ChannelRegistry(profile.paths.channels)
        self._names = DisplayNameResolver(client)
        self._notices = NoticeCatalog()
        # Pre-send recheck and the send queue must see the same consumption
        # record, or the recheck absorbs a message the queue then resends.
        self.consumption = ThreadConsumption()
        # Worker and the state snapshot must share this counter, or the
        # state file always reports 0 in-flight.
        self.inflight = InflightCounter()
        self._started_at = time.time()
        self._shutting_down = False

        self._engine: Engine | None = None
        self._gateway: SlackGateway | None = None
        self._ingress: IngressService | None = None
        self._pipeline: RequestPipeline | None = None
        self._access_policy: AccessPolicy | None = None
        self._tool_policy: ToolPolicy | None = None
        self._audit: AuditLog | None = None
        self._owner_dm_channel = ""
        self._queue: SqliteJobQueue | None = None
        self._reactions: ReactionMarker | None = None
        self._publisher: MessagePublisher | None = None
        self._review_tasks: dict[str, ReviewTask] | None = None
        self._identity: SlackBotIdentity | None = None
        self._roster_builder: RosterBuilder | None = None
        self._roster_refresher: PeriodicRunner | None = None
        self._connection_watch: SocketErrorWatch | None = None
        self._health_runner: PeriodicRunner | None = None
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
        self._connection_epochs: SqliteConnectionEpochs | None = None
        self._transcript_reader_cache: dict[str, SessionTranscriptReader] = {}
        self._transcript_readers = transcript_readers or TranscriptReaderRegistry()
        self._invoker: EngineInvoker | None = None
        self._watch_jobs: WatchJobQueue | None = None
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
        registry = EngineRegistry()
        registry.register(ClaudeEngine)
        registry.register(CodexEngine)
        registry.register(GeminiEngine)
        return registry

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
                self._invoker = DirectInvoker(runner, primary)
            else:
                secondary = self._registry.create(fallback_spec.type, self._profile, self._settings)
                fallback = FallbackEngine(
                    primary,
                    secondary,
                    EngineSwitcher(self._profile.paths.engine_state),
                    runner,
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
    def access_policy(self) -> AccessPolicy:
        if self._access_policy is None:
            extensions = [e for p in self._plugins for e in p.access_extensions()]
            self._access_policy = AccessPolicy(self._profile, self._channels, extensions)
        return self._access_policy

    @property
    def readable_dirs(self) -> tuple[Path, ...]:
        """Directories every engine call may read. One place so the watch
        check and the normal path cannot drift apart (sca-0ab)."""
        return (self._profile.paths.persona, self._profile.paths.prompts)

    def tool_policy(self) -> ToolPolicy:
        if self._tool_policy is None:
            extensions = [e for p in self._plugins for e in p.access_extensions()]
            self._tool_policy = ToolPolicy(
                self._settings.base_tools, self._settings.owner_tools, extensions,
            )
        return self._tool_policy

    def _prompt_sections(self) -> list[PromptSection]:
        sections: list[PromptSection] = [
            PersonaSection(),
            KnowledgeSection(),
            RosterSection(self._profile.roster_file),
            ChannelModeSection(mode_prompt_names(), default_prompt=DEFAULT_PROMPT),
            OwnerNoteSection(),
            SlackFormatSection(),
            ReviewFormatSection(),
            AskerSection(),
            PresentPeopleSection(),
            TrustedSection(),
            SensitiveGuardSection(),
            AuthoritySection(),
            WatchSection(),
            SilenceRuleSection(),
        ]
        sections.extend(s for p in self._plugins for s in p.prompt_sections())
        return sections

    def _composer(self) -> SystemPromptComposer:
        paths = self._profile.paths
        library = PromptLibrary(
            paths.prompts, placeholders={"OWNER_MENTION": f"<@{self._profile.owner_user_id}>"}
        )
        knowledge = KnowledgeLoader(paths.persona / "PERSONA.md", paths.knowledge)
        return SystemPromptComposer(library, knowledge, self._prompt_sections())

    def _guards(self) -> GuardPipeline:
        guards: list[OutputGuard] = [
            PlainMentionGuard(),
            AddresseeGuard(),
            WatchPromiseGuard(),
            RewriteLossGuard(self._settings),
            # safe to always include — a no-op when the drop-line list is empty
            ConfiguredLineDropGuard(self._settings),
        ]
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
            )
        return self._publisher

    def progress(self) -> ProgressCoordinator:
        """Progress display, built for every bot. Which channels actually get
        one is the `progress` flag in channels.json, read per request."""
        if self._progress is None:
            self._progress = ProgressCoordinator(
                settings=self._settings,
                sink_factory=lambda channel, thread_ts, user: FallbackProgressSink(
                    lambda: SlackStreamingProgressSink(
                        self._client, channel, thread_ts,
                        team_id=self.identity.team_id,
                        user_id=user,
                        bot_display_name=self._profile.display_name,
                    ),
                    lambda: SlackProgressSink(
                        self._client, channel, thread_ts, self._profile.display_name,
                    ),
                ),
                log_dir=self._profile.paths.progress,
            )
        return self._progress

    def _transcript_builder(self) -> TranscriptBuilder:
        return TranscriptBuilder(
            client=self._client,
            settings=self._settings,
            notices=self._notices,
            name_resolver=self._names,
            identity=self.identity,
            bot_display_name=self._profile.display_name,
            owner_user_id=self._profile.owner_user_id,
        )

    def _late_addendum(self) -> LateAddendumChecker:
        # reuses the shared history adapter so history-fetch rate limiting stays centralized
        return LateAddendumChecker(
            self._history_port(),
            self._notices,
            self._names,
            self._settings,
            identity=self.identity,
            bot_display_name=self._profile.display_name,
            owner_user_id=self._profile.owner_user_id,
        )

    def _participants(self) -> ThreadParticipants:
        """Shares the _history_port adapter so history-fetch rate limiting stays
        centralized — otherwise Slack can start returning empty results.
        """
        return ThreadParticipants(
            self._history_port(),
            self._names,
            self.identity.user_id,
            limit=self._settings.history_max_msgs,
        )

    def _linked_threads(self) -> LinkedThreadNote:
        """Reuses _transcript_builder so a linked thread is formatted exactly
        like the current one — a second formatter here would drift.
        """
        return LinkedThreadNote(
            LinkedThreadReader(
                self._transcript_builder(),
                self._channel_display_name,
                max_links=self._settings.linked_thread_max,
            )
        )

    def _channel_display_name(self, channel: str) -> str:
        config = self._channels.get(channel)
        return getattr(config, "name", "") or ""

    def _history_port(self) -> SlackHistoryPort:
        return SlackHistoryPort(HistoryReader(self._client, self._settings), self._client)

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
                session_manager=SessionManager(SqliteSessionStore(self._database), self._settings),
                engine=self.engine,
                invoker=self.engine_invoker,
                guard_pipeline=self._guards(),
                publisher=self.publisher(),
                audit=self.audit(),
                channels=self._channels,
                default_workdir=self._profile.work_root,
                owner_user_id=self._profile.owner_user_id,
                reactions=self.reactions(),
                name_resolver=self._names,
                mention_table=self._names.name_table,
                slow_reporter=self._slow_reporter(),
                # built lazily rather than as a field — eagerly building it here would
                # look up the bot user ID, making assembly alone call out to Slack
                participants=lambda channel, thread_ts: self._participants().of(channel, thread_ts),
                linked_threads=lambda text, channel: self._linked_threads().of(text, channel),
                late_addendum=self._late_addendum(),
                consumption=self.consumption,
                watch_queue=self.watch_jobs(),
                response_archive=self.response_archive(),
                tool_policy=self.tool_policy(),
                readable_dirs=self.readable_dirs,
                progress=self.progress(),
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
                applier=LearningApplier(paths.knowledge, self._profile.display_name),
                renderer=ProposalRenderer(),
                notify=self._notify_owner,
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
        if removed:
            log.info("끝난 작업 정리 : %s건", removed)

    def watch_jobs(self) -> WatchJobQueue:
        # cached and shared so registration, checking, and status reporting all
        # see the same instance — recreating it would work (storage is in the
        # DB) but would hide which paths touch it
        if self._watch_jobs is None:
            self._watch_jobs = WatchJobQueue(self._database)
        return self._watch_jobs

    def watch_checker(self) -> WatchJobChecker:
        # owner notification needs an owner to send to; the DM channel itself
        # is resolved at send time (see owner_dm_channel)
        return WatchJobChecker(
            queue=self.watch_jobs(),
            run_check=self._watch_run_check,
            results=WatchResultReader(),
            publisher=self.publisher(),
            channels=self._channels,
            settings=self._settings,
            reactions=self.reactions(),
            notify_owner=self._notify_owner if self._profile.owner_user_id else None,
            # checks/last_run are current state only. Without this there is no
            # record that a watch ever ran (sca-j3d).
            audit=self.audit(),
        )

    def watch_result_cleanup_runner(self) -> PeriodicRunner:
        # A run_id is issued per request, so work started without a watch tag --
        # or whose registration failed -- leaves .watch-out files nobody reads
        # (sca-y6g). No completion path passes those, so this is the only trigger.
        return PeriodicRunner(
            self._watch_result_cleanup_tick,
            self._settings.watch_result_cleanup_interval_sec,
            name="watch_result_cleanup",
        )

    def _watch_result_cleanup_tick(self) -> None:
        reader = WatchResultReader()
        removed = 0
        for workdir in self._watch_result_dirs():
            removed += reader.cleanup(
                str(workdir), older_than_sec=self._settings.watch_result_retain_sec
            )
        if removed:
            log.info("감시 결과 파일 %d개를 정리했다", removed)

    def _watch_result_dirs(self) -> list[Path]:
        """Every workdir a watch job could have run in. _watch_workdir picks
        between the channel's workdir and work_root, so both are swept."""
        dirs = {self._profile.work_root}
        for config in self._channels.all().values():
            if config.workdir:
                dirs.add(config.workdir)
        return sorted(dirs)

    def watch_runner(self) -> PeriodicRunner:
        # without this runner, watch jobs get registered but never checked
        return PeriodicRunner(
            self.watch_checker().check_once,
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
        결과파일 = WatchResultReader().path_for(str(workdir), job.run_id)
        prompt = watch_check_prompt(job.condition, outcome, str(결과파일) if 결과파일 else "")
        system_prompt = self._composer().compose(CompositionContext(
            principal=principal,
            prompt=prompt,
            channel_mode=config.mode if config else "default",
            channel_slug=config.name if config else job.channel,
            is_rich=bool(config and config.rich),
            chat_level=config.chat if config else "normal",
            # This turn only looks: the registration guidance would tell it how
            # to start new work, which watch_check_prompt forbids (sca-ejy).
            watch_check=True,
        ))
        return self.engine_invoker.invoke(EngineRequest(
            prompt=prompt,
            system_prompt=system_prompt,
            # Left empty on purpose: the engine that ends up running this
            # check mints one in its own format (sca-56y).
            session_id=None,
            resume=False,
            model=self.access_policy.model_for(principal),
            effort=self.access_policy.effort_for(principal, prompt),
            workdir=workdir,
            # readonly: the check only looks. Owner extras and Skill would let
            # this turn start new work, which `watch_check_prompt` forbids.
            # Left empty before, and claude turns an empty list into
            # `--allowedTools ""` — a turn with no tools at all (sca-0ab).
            allowed_tools=self.tool_policy().tool_list_for(
                principal, prompt=prompt, readonly=True, skills_enabled=False,
            ),
            readable_dirs=self.readable_dirs,
            trust_level=job.trust,
        ), CallOrigin.BACKGROUND)

    def mark_shutting_down(self) -> None:
        # without this flag, anything reading the state file can't tell a
        # stopped process from one that's shutting down
        self._shutting_down = True

    def _snapshot_source(self) -> ApplicationSnapshotSource:
        return ApplicationSnapshotSource(
            inflight=self.inflight,
            queue=self.queue(),
            socket_watch=self.connection_watch(),
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
            )
        return self._catchup_service

    def worker(self, worker_id: str = "worker") -> Worker:
        # unlike the other components here, a new Worker is created on every
        # call — worker_id must differ per process
        return Worker(
            queue=self.queue(),
            handler=self.pipeline(),
            heartbeat=WorkerHeartbeat(self.queue(), self._settings),
            catchup=self._catchup(),
            markers=self.reactions(),
            settings=self._settings,
            worker_id=worker_id,
            inflight=self.inflight,
        )

    def _admin_router(self) -> AdminRouter:
        commands = default_admin_commands(self._notices)
        commands.extend(c for p in self._plugins for c in p.admin_commands())
        return AdminRouter(commands)

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
        # admin command replies are always plain text, regardless of channel rich-mode config
        self.publisher().post(channel, thread_ts, message, rich=False)

    def ingress(self) -> IngressService:
        if self._ingress is None:
            self._ingress = IngressService(
                listener=EventListener(
                    client=self._client,
                    channel_registry=self._channels,
                    gate=ResponseGate(),
                    identity=self.identity,
                ),
                dedup=DeduplicationTracker(),
                queue=self.queue(),
                reactions=self.reactions(),
                attachments=self.attachments(),
                admin_router=self._admin_router(),
                admin_context_builder=self._admin_context,
                reply=self._reply,
                allowed_reactions=self.allowed_reactions(),
                on_reaction=self.on_reaction,
                spawn=ThreadTaskSpawner(),
                # failed jobs get resurrected via re-registration; capping attempts
                # keeps a permanently-failing request from reviving on every redelivery
                job_max_attempts=self._settings.job_max_attempts,
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
                "engine": self._review_engine(),
                "troubleshoot_channel": self._profile.troubleshoot_channel,
                # 점검은 몇 분이 걸린다. 표시가 없으면 도는 것과 죽은 것이
                # 사용자에게 같아 보인다(sca-tfd).
                "progress": ReviewProgressDisplay(self.progress(), self._channels),
                # 점검 한 건이 엔진을 몇 분씩 쓴다. 남기지 않으면 제한시간을
                # 어떻게 잡을지 정할 근거가 없다(sca-fy5).
                "audit": self.audit(),
            }
            paths = self._profile.paths
            owner_name = self._names.resolve(self._profile.owner_user_id)
            self._review_tasks = {
                POSTMORTEM_EMOJI: PostmortemTask(
                    bot_display_name=self._profile.display_name,
                    code_dir=str(self._profile.work_root),
                    persona_dir=str(paths.persona),
                    **shared,
                ),
                DEBUG_TRACE_EMOJI: DebugTraceTask(
                    bot_display_name=self._profile.display_name,
                    code_dir=str(self._profile.work_root),
                    persona_dir=str(paths.persona),
                    owner_display_name=owner_name,
                    **shared,
                ),
                FORMAT_REVIEW_EMOJI: FormatReviewTask(
                    bot_display_name=self._profile.display_name,
                    persona_dir=str(paths.persona),
                    prompts_dir=str(paths.prompts),
                    post_rich_command="리치",
                    **shared,
                ),
            }
        return self._review_tasks

    def _review_engine(self) -> ReviewEngineCaller:
        paths = self._profile.paths
        return ReviewEngineCaller(
            # passing the runner directly here would also skip fallback-switch handling
            invoker=self.engine_invoker,
            profile=self._profile,
            workdir=self._profile.work_root,
            # empty — each ReviewTask.build_prompt embeds its own instructions in the user turn
            system_prompt="",
            readable_dirs=(paths.persona, paths.prompts),
        )

    def on_reaction(self, emoji: str, channel: str, ts: str, by_user: str) -> None:
        """Catches and logs review-task failures instead of propagating —
        otherwise one bad review event would block all future ones, and a
        swallowed failure would look identical to one that was never triggered.
        """
        task = self.review_tasks().get(emoji)
        if task is None:
            return
        config = self._channels.get(channel)
        try:
            task.run(
                ReviewTarget(
                    channel=channel,
                    ts=ts,
                    by_user=by_user,
                    channel_name=getattr(config, "name", "") or channel,
                    rich=bool(getattr(config, "rich", False)),
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

    def _slow_reporter(self) -> SlowRequestReporter:
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
            settings=self._settings,
            troubleshoot_channel=self._profile.troubleshoot_channel,
        )

    def roster_builder(self) -> RosterBuilder:
        """Not built in __init__ — that would make assembly itself require a
        Slack call, tying pre-startup checks to the network. Cached so the
        periodic refresher and direct calls write through the same instance.
        """
        if self._roster_builder is None:
            self._roster_builder = RosterBuilder(self._client, self._profile.roster_file)
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
            self.attachments().cleanup,
            self._settings.attachment_cleanup_interval_sec,
            name="attachment_cleanup",
        )

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

    def _report_recovery(self, outage_sec: float, recovered: int) -> None:
        # reports the recovered count explicitly — otherwise zero recovered
        # messages would look the same as catch-up never having run
        self._notify_owner(
            "*연결 복구*\n\n"
            f"- 끊긴 시간 : {outage_sec / 60:.0f}분\n"
            f"- 복구 시각 : {datetime.now(KST).strftime('%m-%d %H:%M:%S')} KST\n"
            f"- 캐치업으로 처리한 요청 : {recovered}건"
        )

    def _catchup_retry_tick(self, worker: Worker) -> None:
        outage_sec = self.outage_tracker().check()
        if outage_sec is not None:
            # events during the outage never re-arrive via the socket, so we
            # must catch up now or lose them permanently
            log.info("슬랙 연결이 돌아왔다. 끊긴 시간 %.0f초", outage_sec)
            report = worker.catch_up(
                self.channel_ids(), window_sec=self._recovery_window_sec(outage_sec)
            )
            self._report_recovery(outage_sec, len(report.missed))

        for status in worker.retry_catchup():
            if not status.alert:
                continue
            # silently retrying forever would hide a channel stuck for hours from anyone
            self._notify_owner(
                "*캐치업을 오래 마치지 못하고 있습니다*\n\n"
                f"- 채널 : {status.channel}\n"
                f"- {status.stuck_sec / 60:.0f}분째입니다\n\n"
                "슬랙이 채널 기록을 계속 빈 목록으로 돌려줍니다.\n"
                "그 채널에서 답을 기다리는 요청이 있어도 잡히지 않습니다."
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
                self.attachment_cleanup_runner(),
                self.pending_report_runner(),
                self.stale_review_runner(),
            ],
            name="ingress",
        )

    def worker_services(self, worker: Worker) -> ServiceGroup:
        # catchup retry enqueues onto this specific worker's queue, hence the worker argument
        return ServiceGroup(
            [
                self.state_snapshot_runner(),
                self.watch_runner(),
                self.watch_result_cleanup_runner(),
                self.job_purge_runner(),
                self.startup_catchup_runner(worker),
                self.connection_catchup_runner(worker),
                self.catchup_retry_runner(worker),
                self.pending_report_runner(),
                self.learning_batch_runner(),
            ],
            name="worker",
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
