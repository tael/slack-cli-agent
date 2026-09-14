"""Application — Profile 하나에서 전체 객체 그래프를 만든다.

이 패키지의 부품은 전부 생성자 주입이다. 어떤 부품도 자기가 쓸 협력자를 직접
만들지 않는다. 그 결정을 한 곳에 모은 것이 이 파일이다.

원본 `bot.py` 는 모듈을 import 하는 순간 전역에 슬랙 client·DB 연결·프롬프트
경로가 만들어졌다. 그래서 무엇 하나를 시험하려면 그 전부가 딸려 왔다. 여기서는
조립을 클래스 하나로 옮겨, 조립 자체를 시험할 수 있게 하고 프로세스마다
필요한 부분만 만들게 한다.

프로세스는 둘로 갈린다. 접수(`ingress()`)는 슬랙 이벤트를 받아 큐에 넣기만
하고, 처리(`worker()`)는 큐를 소비해 엔진을 부른다. 둘은 같은 DB 를 쓰지만
서로 다른 프로세스로 뜬다 — 접수가 엔진 호출에 막혀 이벤트를 놓치는 일이
없어야 한다.

부품 생성은 지연이고 결과는 캐시한다. 특히 게이트웨이가 부를 때마다 새로
만들어지면 핸들러를 등록한 객체와 연결을 맺는 객체가 갈라져, 이벤트가 들어와도
아무 핸들러도 불리지 않는다.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Sequence
from typing import Any

from ..admin.channel_commands import (
    ApiModeCommand,
    ChannelUnregisterCommand,
    ChatActiveCommand,
    ChatNormalCommand,
    ChatQuietCommand,
    CoachModeCommand,
    DefaultModeCommand,
)
from ..admin.command import AdminContext
from ..admin.commands import ChannelListCommand, EngineStatusCommand, HelpCommand
from ..admin.engine_commands import EngineApproveCommand, EngineDenyCommand
from ..admin.learning_commands import (
    LearningApplyCommand,
    LearningRevertCommand,
    LearningShowCommand,
)
from ..admin.router import AdminRouter
from ..auth.policy import AccessPolicy
from ..auth.principal import Principal, TrustLevel
from ..config.channel import ChannelRegistry
from ..config.profile import Profile
from ..config.settings import RuntimeSettings
from ..engine.base import Engine, EngineRequest, EngineResponse
from ..engine.claude import ClaudeEngine
from ..engine.codex import CodexEngine
from ..engine.environment import create_environment_policy
from ..engine.registry import EngineRegistry
from ..engine.runner import (
    DirectInvoker,
    EngineInvoker,
    EngineRunner,
    FallbackEngine,
    FallbackInvoker,
)
from ..engine.switcher import EngineSwitcher
from ..engine.transcript import ClaudeTranscriptReader
from ..guard.base import OutputGuard
from ..guard.dropline import ConfiguredLineDropGuard
from ..guard.mentions import AddresseeGuard, PlainMentionGuard
from ..guard.pipeline import GuardPipeline
from ..guard.rewrite import RewriteLossGuard
from ..guard.watch import WatchPromiseGuard
from ..jobs.heartbeat import WorkerHeartbeat
from ..jobs.queue import SqliteJobQueue
from ..observability.app_snapshot import ApplicationSnapshotSource
from ..observability.audit import AuditLog
from ..observability.notices import NoticeCatalog
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
from ..reliability.dedup import DeduplicationTracker
from ..reliability.health import HealthMonitor, SelfRestarter, SocketErrorWatch
from ..reliability.outage import OutageTracker
from ..reliability.pending_report import PendingReportStore
from ..reliability.watchjobs import WatchJob, WatchJobQueue
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
from ..review.trace import DebugTraceTask
from ..session.manager import SessionManager
from ..session.store import SqliteSessionStore
from ..slack.attachments import AttachmentStore, DownloadResult
from ..slack.download import HttpDownloader
from ..slack.gate import ResponseGate
from ..slack.gateway import SlackGateway
from ..slack.history import HistoryReader
from ..slack.history_port import SlackHistoryPort
from ..slack.identity import BotIdentity, SlackBotIdentity
from ..slack.late_addendum import LateAddendumChecker, ThreadConsumption
from ..slack.listener import EventListener
from ..slack.names import DisplayNameResolver
from ..slack.participants import ThreadParticipants
from ..slack.publisher import MessagePublisher
from ..slack.reactions import (
    DEBUG_TRACE_EMOJI,
    FORMAT_REVIEW_EMOJI,
    POSTMORTEM_EMOJI,
    ReactionMarker,
)
from ..slack.review_ports import (
    ReviewPublisher,
    SlackMessageLookup,
    SlackPermalinks,
    ThreadTranscriptPort,
)
from ..slack.roster import RosterBuilder
from ..slack.transcript import TranscriptBuilder
from ..storage.database import Database
from .context import RequestContext
from .ingress import IngressService
from .lifecycle import InflightCounter
from .periodic import PeriodicRunner
from .pipeline import RequestPipeline
from .services import ServiceGroup
from .worker import Worker

log = logging.getLogger(__name__)

# 채널 mode 하나에 프롬프트 파일 하나가 대응한다. 모드가 늘어도 이 표를 고치지
# 않도록 이름 규약으로 만든다 — mode "agent_coach" 는 prompts/prompt_agent_coach.md 다.
DEFAULT_PROMPT = "PROMPT_DEFAULT"
KNOWN_MODES: tuple[str, ...] = ("private", "agent_coach", "api_helpdesk")

# 소켓 연결 상태를 로그로 알리는 라이브러리. 연결 감시를 여기에 붙인다.
SOCKET_LOGGERS: tuple[str, ...] = ("slack_sdk.socket_mode", "slack_bolt")


def mode_prompt_names(modes: Sequence[str] = KNOWN_MODES) -> dict[str, str]:
    return {mode: f"PROMPT_{mode.upper()}" for mode in modes}


class Application:
    """봇 하나를 이루는 객체 전부를 만들고 서로 이어 준다."""

    def __init__(
        self,
        profile: Profile,
        client: Any,
        *,
        settings: RuntimeSettings | None = None,
        plugins: Sequence[BotPlugin] | None = None,
        database: Database | None = None,
        engine_registry: EngineRegistry | None = None,
        token_provider: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._profile = profile
        self._client = client
        # 신원 재조회 간격을 재는 시계. 시험이 시간을 제어할 수 있게 주입받는다.
        self._clock = clock
        profile.paths.ensure()

        base = settings or RuntimeSettings()
        self._settings = base.override(profile.settings_override)

        self._plugins = tuple(plugins) if plugins is not None else self._load_plugins()
        self._token_provider = token_provider or (lambda: "")

        self._database = database or Database(profile.paths.database)
        self._database.migrate()

        self._registry = engine_registry or self._default_registry()

        self._channels = ChannelRegistry(profile.paths.channels)
        self._names = DisplayNameResolver(client)
        self._notices = NoticeCatalog()
        # 발송 전 재확인과 대기줄이 같은 소화 기록을 봐야 한다. 따로 만들면
        # 재확인이 흡수한 말을 대기줄이 또 돌려 같은 답이 두 번 올라간다.
        self.consumption = ThreadConsumption()
        # 진행 중 건수는 워커와 상태 기록이 같은 값을 봐야 한다. 따로 만들면
        # 상태 파일이 늘 0 으로 남는다.
        self.inflight = InflightCounter()
        self._started_at = time.time()
        self._shutting_down = False

        # 지연 생성물
        self._engine: Engine | None = None
        self._gateway: SlackGateway | None = None
        self._ingress: IngressService | None = None
        self._pipeline: RequestPipeline | None = None
        self._access_policy: AccessPolicy | None = None
        self._queue: SqliteJobQueue | None = None
        self._reactions: ReactionMarker | None = None
        self._publisher: MessagePublisher | None = None
        self._review_tasks: dict[str, ReviewTask] | None = None
        # 이 봇의 신원. 자기 말 판정이 필요한 부품 전부가 이 하나를 본다.
        self._identity: SlackBotIdentity | None = None
        self._roster_builder: RosterBuilder | None = None
        self._roster_refresher: PeriodicRunner | None = None
        self._connection_watch: SocketErrorWatch | None = None
        # 연결 점검기와 그 주기 실행기. 점검기는 끊김 시작 시각을 안에
        # 들고 있어, 회차마다 새로 만들면 복구 판정이 나오지 않는다.
        self._health_runner: PeriodicRunner | None = None
        self._attachments: AttachmentStore | None = None
        self._catchup_service: CatchupService | None = None
        self._pending_report: PendingReportStore | None = None
        self._outage_tracker: OutageTracker | None = None
        # 엔진 실행 부품. 엔진을 만들 때 함께 정한다 — 폴백이 설정돼
        # 있는지는 그 시점에만 드러나고, 나중에 종류로 되짚으면 조립이
        # 무엇을 만들었는지가 코드에서 사라진다.
        self._invoker: EngineInvoker | None = None
        self._watch_jobs: WatchJobQueue | None = None
        self._closed = False

    # -- 기본 부품 -------------------------------------------------

    @classmethod
    def from_profile(cls, profile: Profile, client: Any | None = None, **kwargs: Any) -> Application:
        """슬랙 client 를 여기서 만든다. 시험은 이 경로를 쓰지 않는다."""
        if client is None:
            from slack_sdk import WebClient  # 지연 import — 조립 시험이 SDK 를 끌어오지 않는다

            client = WebClient(token=_bot_token())
        kwargs.setdefault("token_provider", _bot_token)
        return cls(profile, client, **kwargs)

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
        return registry

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

    # -- 엔진 ------------------------------------------------------

    @property
    def engine(self) -> Engine:
        """1차 엔진. 폴백이 설정돼 있으면 FallbackEngine 으로 감싼다."""
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
                # 실행기를 그대로 넘기면 이 클래스의 전환 판정이 건너뛰어진다.
                self._invoker = FallbackInvoker(fallback)
        return self._engine

    @property
    def engine_invoker(self) -> EngineInvoker:
        """엔진 실행 한 걸음. 호출부는 폴백 여부를 모른다."""
        if self._invoker is None:
            # engine 프로퍼티가 부수효과로 self._invoker 를 채운다.
            _ = self.engine
        assert self._invoker is not None
        return self._invoker

    @property
    def engine_runner(self) -> EngineRunner:
        """엔진 실행기. 환경 격리 정책을 함께 넣는다.

        정책 없이 만들면 엔진 하위 프로세스가 이 프로세스의 환경을 통째로
        물려받는다 — 슬랙 토큰과 다른 엔진의 자격증명이 그대로 넘어간다.
        정책은 1차 엔진 종류를 따른다.
        """
        spec = self._profile.primary_engine
        return EngineRunner(
            self._settings,
            environment_policy=create_environment_policy(
                spec.type, self._profile.name, spec.home_dir,
            ),
        )

    # -- 권한·프롬프트 ---------------------------------------------

    @property
    def access_policy(self) -> AccessPolicy:
        if self._access_policy is None:
            extensions = [e for p in self._plugins for e in p.access_extensions()]
            self._access_policy = AccessPolicy(self._profile, self._channels, extensions)
        return self._access_policy

    def _prompt_sections(self) -> list[PromptSection]:
        """조각 순서가 시스템 프롬프트의 순서다. 원본 build_system_prompt 순서를 따른다."""
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
            # 설정에 적은 문구로 시작하는 줄을 지운다. 목록이 비어 있으면
            # 아무것도 안 지우므로 기본 조립에 그대로 둔다.
            ConfiguredLineDropGuard(self._settings),
        ]
        guards.extend(g for p in self._plugins for g in p.output_guards())
        return GuardPipeline(guards)

    # -- 슬랙 ------------------------------------------------------

    def gateway(self) -> SlackGateway:
        if self._gateway is None:
            self._gateway = SlackGateway(self._client)
        return self._gateway

    def reactions(self) -> ReactionMarker:
        if self._reactions is None:
            self._reactions = ReactionMarker(self._client)
        return self._reactions

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
            )
        return self._publisher

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
        """발송 직전 스레드 재확인. 기록 조회는 다른 부품과 같은 어댑터를 쓴다."""
        return LateAddendumChecker(
            self._history_port(),
            self._notices,
            self._names,
            self._settings,
            owner_user_id=self._profile.owner_user_id,
        )

    def _participants(self) -> ThreadParticipants:
        """스레드에 함께 있는 사람을 추리는 부품.

        기록 조회는 `_history_port` 와 같은 어댑터를 쓴다 — 조회 간격 제한을
        한 곳에서 지켜야 슬랙이 빈 응답을 돌려주는 것을 막는다.
        """
        return ThreadParticipants(
            self._history_port(),
            self._names,
            self.identity.user_id,
            limit=self._settings.history_max_msgs,
        )

    def _history_port(self) -> SlackHistoryPort:
        return SlackHistoryPort(HistoryReader(self._client, self._settings), self._client)

    # -- 큐·파이프라인·워커 ----------------------------------------

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
                audit=AuditLog(self._database, self._profile.paths.audit_log),
                channels=self._channels,
                default_workdir=self._profile.work_root,
                owner_user_id=self._profile.owner_user_id,
                reactions=self.reactions(),
                name_resolver=self._names,
                mention_table=self._names.name_table,
                slow_reporter=self._slow_reporter(),
                # 부품을 여기서 만들지 않고 호출 시점에 만든다. 봇 사용자 ID
                # 조회가 들어 있어, 조립만으로 슬랙을 부르게 된다.
                participants=lambda channel, thread_ts: self._participants().of(channel, thread_ts),
                late_addendum=self._late_addendum(),
                consumption=self.consumption,
                watch_queue=self.watch_jobs(),
            )
        return self._pipeline

    def job_purge_runner(self) -> PeriodicRunner:
        """끝난 작업을 주기적으로 지운다. 안 띄우면 jobs 표가 계속 커진다.

        완료·실패 행은 중복 방어 기록이기도 하다. 되짚기 최대 창보다 오래
        남겨야 이미 답한 메시지를 미응답으로 다시 집지 않는다.
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
        """감시 큐. 등록·확인·상태 기록이 같은 객체를 본다.

        매번 새로 만들면 기능은 같지만(저장은 DB 에 있다) 어느 경로가 무엇을
        보는지가 조립에서 안 드러난다. 하나로 두고 공유한다.
        """
        if self._watch_jobs is None:
            self._watch_jobs = WatchJobQueue(self._database)
        return self._watch_jobs

    def watch_checker(self) -> WatchJobChecker:
        """등록된 감시 건을 한 회차 확인하는 실행기.

        소유자 통지는 개인 대화가 설정돼 있을 때만 넘긴다. 없으면 포기 건을
        알릴 곳이 없으므로 통지 없이 완료 표시만 한다.
        """
        return WatchJobChecker(
            queue=self.watch_jobs(),
            run_check=self._watch_run_check,
            publisher=self.publisher(),
            channels=self._channels,
            settings=self._settings,
            reactions=self.reactions(),
            notify_owner=self._notify_owner if self._profile.owner_dm else None,
        )

    def watch_runner(self) -> PeriodicRunner:
        """감시 확인을 주기적으로 실행한다. 안 띄우면 등록만 되고 확인이 없다."""
        return PeriodicRunner(
            self.watch_checker().check_once,
            self._settings.watch_check_interval_sec,
            name="watch_jobs",
        )

    def pending_report(self) -> PendingReportStore:
        """보내지 못한 보고를 남겨 두는 자리. 한 번 만들어 계속 쓴다."""
        if self._pending_report is None:
            self._pending_report = PendingReportStore(
                path=self._profile.state_dir / "pending_report.json",
                sender=self._post_owner_dm,
            )
        return self._pending_report

    def pending_report_runner(self) -> PeriodicRunner:
        """남겨둔 보고를 주기적으로 다시 보낸다.

        기동 시 한 번만 보내면 그 뒤에 생긴 보고는 다음 재기동까지 파일에
        남는다. 원본에서 실제로 되짚기 실패 보고가 139분 동안 전달되지 않았다.
        """
        return PeriodicRunner(
            self.pending_report().flush,
            self._settings.pending_report_flush_interval_sec,
            name="pending_report",
        )

    def _post_owner_dm(self, text: str) -> None:
        self.publisher().post(self._profile.owner_dm, "", text, False)

    def _notify_owner(self, text: str) -> None:
        """소유자 개인 대화로 알린다.

        발송이 실패하면 그 보고를 파일에 남긴다. 로그만 남기고 끝내면 운영자는
        장애가 났다는 사실 자체를 못 받는다 — 특히 재기동 사유를 알리는 그
        순간은 소켓이 불안정해 발송이 실패하기 쉬운 시점이다.
        """
        try:
            self._post_owner_dm(text)
        except Exception as exc:  # noqa: BLE001 — 발송 실패 원인이 슬랙 SDK 예외부터 네트워크 오류까지 다양하다. 어떤 실패든 보고를 남기는 것이 목적이다
            log.warning("소유자 알림 발송 실패, 보고를 남긴다 : %s", exc)
            self.pending_report().save(text)

    def _watch_run_check(self, job: WatchJob) -> EngineResponse:
        """감시 확인 한 건을 엔진으로 실행한다.

        새 세션으로 돈다 — 확인은 등록보다 한참 뒤에 일어나 원래 대화 세션이
        이미 만료됐을 수 있고, 없는 세션으로 이어받기를 시도하면 그 실행 자체가
        실패한다. 원본도 확인마다 새 세션 ID 를 쓴다.

        권한은 등록 시점 값을 그대로 이어받는다. 여기서 낮추면 소유자 권한으로
        등록된 건이 확인 단계에서 조회에 실패한다.
        """
        principal = Principal(
            user_id=self._profile.owner_user_id if job.trust is TrustLevel.OWNER else "",
            channel=job.channel,
            trust=job.trust,
            is_direct_message=job.channel.startswith("D"),
        )
        config = self._channels.get(job.channel)
        prompt = watch_check_prompt(job.condition)
        system_prompt = self._composer().compose(CompositionContext(
            principal=principal,
            prompt=prompt,
            channel_mode=config.mode if config else "default",
            channel_slug=config.name if config else job.channel,
            is_rich=bool(config and config.rich),
            chat_level=config.chat if config else "normal",
        ))
        return self.engine_invoker.invoke(EngineRequest(
            prompt=prompt,
            system_prompt=system_prompt,
            session_id=uuid.uuid4().hex,
            resume=False,
            model=self.access_policy.model_for(principal),
            effort=self.access_policy.effort_for(principal, prompt),
            workdir=(config.workdir if (config and config.workdir) else self._profile.work_root),
            trust_level=job.trust,
        ))

    def mark_shutting_down(self) -> None:
        """종료 절차가 시작됐음을 상태 기록에 반영한다.

        이 값이 없으면 상태 파일만 보는 쪽이 멈춘 프로세스와 종료 중인
        프로세스를 구분하지 못한다.
        """
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
        """상태 기록을 주기적으로 갈아 끼우는 실행기.

        주기는 헬스 점검과 같다 — 그보다 자주 써도 읽는 쪽이 못 따라간다.
        """
        return PeriodicRunner(
            self.state_snapshot_writer().write,
            self._settings.health_interval_sec,
            name="state_snapshot",
        )

    def _catchup(self) -> CatchupService:
        """되짚기 서비스. 한 번 만들어 계속 쓴다.

        마치지 못한 채널과 재시도 횟수를 이 객체가 들고 있다. 회차마다 새로
        만들면 그 기록이 매번 비어 재시도 간격이 항상 첫 회차 값이 된다.
        """
        if self._catchup_service is None:
            self._catchup_service = CatchupService(
                history=self._history_port(),
                gate=ResponseGate(),
                notices=self._notices,
                settings=self._settings,
                bot_user_id=self.identity.user_id,
                is_self=self._is_self_message,
            )
        return self._catchup_service

    def worker(self, worker_id: str = "worker") -> Worker:
        """워커는 부를 때마다 새로 만든다. worker_id 가 프로세스마다 달라야 한다."""
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

    # -- 접수 ------------------------------------------------------

    def _admin_router(self) -> AdminRouter:
        commands = [
            HelpCommand(),
            ChannelListCommand(),
            EngineStatusCommand(),
            EngineApproveCommand(),
            EngineDenyCommand(),
            ChatActiveCommand(self._notices),
            ChatNormalCommand(self._notices),
            ChatQuietCommand(self._notices),
            CoachModeCommand(self._notices),
            ApiModeCommand(self._notices),
            DefaultModeCommand(self._notices),
            ChannelUnregisterCommand(self._notices),
            LearningShowCommand(),
            LearningApplyCommand(),
            LearningRevertCommand(),
        ]
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
        """관리 명령 응답. 채널 설정과 무관하게 평문으로 낸다."""
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
                # 실패로 끝난 건은 재등록으로 되살아난다. 상한을 함께 넘겨
                # 계속 실패하는 요청이 재전달마다 되살아나지 않게 한다.
                job_max_attempts=self._settings.job_max_attempts,
            )
        return self._ingress

    def _download(self, url: str, token: str) -> DownloadResult:
        """AttachmentStore 가 부르는 서명에 맞춘다.

        건네받은 ``token`` 을 쓰지 않는다. ``HttpDownloader`` 가 같은
        ``token_provider`` 에서 매 호출마다 직접 얻으므로 값이 같고, 토큰을
        인자로 옮기는 경로를 하나 줄이면 그만큼 로그·예외에 새어 나갈
        자리가 준다.
        """
        return HttpDownloader(self._token_provider)(url)

    # -- 점검 리액션 -----------------------------------------------

    def allowed_reactions(self) -> frozenset[str]:
        return frozenset(self.review_tasks())

    def review_tasks(self) -> dict[str, ReviewTask]:
        """리액션 이모지 하나에 점검 하나가 대응한다."""
        if self._review_tasks is None:
            shared = {
                "ledger": ReviewLedger(self._database),
                "message_lookup": SlackMessageLookup(self._client),
                "transcript": ThreadTranscriptPort(self._transcript_builder()),
                "answer_finder": AnswerRecordFinder(self._database),
                "reactions": self.reactions(),
                "permalinks": SlackPermalinks(self._client),
                "publisher": ReviewPublisher(self.publisher()),
                "engine": self._review_engine(),
                "troubleshoot_channel": self._profile.troubleshoot_channel,
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
        """점검은 소유자 권한으로 돈다. 시스템 프롬프트는 비운다 —

        점검 지침 전부를 각 ReviewTask 의 `build_prompt` 가 본문에 담는다.
        """
        paths = self._profile.paths
        return ReviewEngineCaller(
            # 실행기를 그대로 넘기면 폴백이 설정돼 있어도 전환 판정이 건너뛰어진다.
            invoker=self.engine_invoker,
            profile=self._profile,
            workdir=self._profile.work_root,
            system_prompt="",
            readable_dirs=(paths.persona, paths.prompts),
        )

    def on_reaction(self, emoji: str, channel: str, ts: str, by_user: str) -> None:
        """점검 리액션 하나를 처리한다.

        한 건의 실패가 이후 이벤트 처리를 막지 않는다. 다만 삼키되 기록은
        남긴다 — 안 남기면 점검이 실패한 것과 아예 안 불린 것이 같은
        모습이 된다.
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

    # -- 연결 감시 -------------------------------------------------

    def _slow_reporter(self) -> SlowRequestReporter:
        """느린 요청의 구간별 시간 분해를 보고 채널에 올리는 객체.

        세션 기록 형식은 엔진마다 다르므로 파서를 주입한다. 보고 채널이 빈
        프로필이 정상이다 — 그때는 보고기가 기준값 판정 전에 넘어간다.
        """
        reader = ClaudeTranscriptReader(self._profile.work_root)
        return SlowRequestReporter(
            publisher=self.publisher(),
            calculator=TimeBreakdownCalculator(reader, self._settings.assumed_tokens_per_sec),
            # 사용량 행 조립기를 여기서 만든다. 기본값으로 두면 세션 컨텍스트
            # 계산기가 없어 "세션" 행이 아예 안 나온다.
            usage_row_builder=UsageRowBuilder(
                self._settings.owner_only_channels,
                SessionContextCalculator(reader, self._settings.context_limit),
            ),
            diagnostician=ElapsedDiagnostician(self._settings.sleep_gap_suspect_sec),
            formatter=SlowReportFormatter(self._settings.assumed_tokens_per_sec),
            settings=self._settings,
            troubleshoot_channel=self._profile.troubleshoot_channel,
        )

    def roster_builder(self) -> RosterBuilder:
        """계정 핸들과 실명을 잇는 명부를 만드는 객체.

        생성자에서 만들지 않는다 — 조립만으로 슬랙 클라이언트를 요구하면
        기동 전 점검이 네트워크에 매인다. 캐시하는 이유는 갱신기와 직접
        호출이 같은 출력 경로를 쓰게 하기 위해서다.
        """
        if self._roster_builder is None:
            self._roster_builder = RosterBuilder(self._client, self._profile.roster_file)
        return self._roster_builder

    def roster_refresher(self) -> PeriodicRunner:
        """명부를 주기적으로 다시 만드는 실행기. 시작은 호출하는 쪽이 한다.

        여기서 시작하지 않는다. 접수 프로세스와 워커가 같은 애플리케이션을
        조립하는데, 양쪽에서 갱신하면 같은 파일을 동시에 쓴다. 어느 프로세스가
        맡을지는 조립이 아니라 그 프로세스의 결정이다.

        캐시한다. 매번 새로 만들면 시작한 객체와 정지를 요청받는 객체가 달라져,
        정지시켜도 먼저 시작된 스레드가 계속 돈다.
        """
        if self._roster_refresher is None:
            self._roster_refresher = PeriodicRunner(
                self.roster_builder().refresh,
                self._settings.roster_refresh_sec,
                name="roster",
            )
        return self._roster_refresher

    def connection_watch(self) -> SocketErrorWatch:
        """소켓 라이브러리 로거에 감시 핸들러를 붙이고 그것을 돌려준다.

        `SocketErrorWatch` 는 `logging.Handler` 다. 어느 로거에 붙이는지는
        만드는 쪽이 아니라 조립의 몫이고, 안 붙이면 소켓이 끊겨도 아무것도
        세지 않는다 — 감시가 있는데 미발동인 것과 감시가 아예 없는 것이
        같은 모습이 된다.

        기본 연결기는 `slack_sdk` 의 Socket Mode 구현을 쓰지만, `slack_bolt`
        로거에도 함께 붙인다. `SlackGateway` 는 연결기를 주입받으므로 bolt
        기반 커넥터를 넣는 조립에서도 같은 감시가 동작해야 한다.
        """
        if self._connection_watch is None:
            watch = SocketErrorWatch()
            for name in SOCKET_LOGGERS:
                logging.getLogger(name).addHandler(watch)
            self._connection_watch = watch
        return self._connection_watch

    def health_monitor(self, restart: Callable[[str], None]) -> HealthMonitor:
        """연결 점검기. `check()` 한 번이 감시 루프의 한 회차다.

        재기동은 프로세스를 실제로 죽이는 동작이라 여기서 하지 않는다 —
        사유만 `restart` 콜백에 넘긴다.
        """
        return HealthMonitor(
            watch=self.connection_watch(),
            reachable=self._slack_reachable,
            restart=restart,
            settings=self._settings,
        )

    def health_runner(self, restart: Callable[[str], None]) -> PeriodicRunner:
        """연결 점검을 주기적으로 실행한다. 안 띄우면 판정 자체가 안 돈다.

        소켓 오류를 세는 핸들러를 로거에 붙이는 것과, 그 값이 상한을 넘었는지
        보는 것은 다른 일이다. 이 실행기가 없으면 오류가 아무리 쌓여도 재기동이
        발화하지 않는다.

        점검기를 한 번 만들어 계속 쓴다. 회차마다 새로 만들면 `_down_since` 가
        매번 비어 있어 끊겼다 돌아온 것을 복구로 판정하지 못한다.
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
        """첨부 저장소. 한 번 만들어 계속 쓴다.

        접수와 정리 실행기가 다른 객체를 보면 저장하는 디렉터리와 지우는
        디렉터리가 갈릴 수 있고, 익명으로 만들면 정리 실행기가 그것을 참조할
        방법이 없다.
        """
        if self._attachments is None:
            self._attachments = AttachmentStore(
                attach_dir=self._profile.attach_dir,
                token_provider=self._token_provider,
                downloader=self._download,
            )
        return self._attachments

    def attachment_cleanup_runner(self) -> PeriodicRunner:
        """오래된 첨부를 주기적으로 지운다. 안 띄우면 받은 파일이 계속 남는다."""
        return PeriodicRunner(
            self.attachments().cleanup,
            self._settings.attachment_cleanup_interval_sec,
            name="attachment_cleanup",
        )

    def catchup_retry_runner(self, worker: Worker) -> PeriodicRunner:
        """마치지 못한 되짚기를 주기적으로 다시 본다.

        슬랙이 채널 기록을 빈 목록으로 주는 것은 대개 잠깐이다. 다시 보지
        않으면 그 구간에 답을 기다리는 요청이 어느 경로에서도 안 잡힌다.
        """
        return PeriodicRunner(
            lambda: self._catchup_retry_tick(worker),
            self._settings.catchup_retry_interval_sec,
            name="catchup_retry",
        )

    def outage_tracker(self) -> OutageTracker:
        """슬랙 도달 여부의 상태 전이. 한 번 만들어 계속 쓴다.

        회차마다 새로 만들면 앞 회차의 결과가 없어 복구를 판정할 수 없다.
        """
        if self._outage_tracker is None:
            self._outage_tracker = OutageTracker(reachable=self._slack_reachable, now=self._clock)
        return self._outage_tracker

    def _recovery_window_sec(self, outage_sec: float) -> float:
        """끊겼던 시간에 맞춰 되짚기 창을 정한다.

        기본 창보다 넓혀야 끊긴 구간의 앞부분이 남지 않는다. 여유 600초는
        끊김을 알아채기까지 걸린 시간을 덮는다. 최대값을 두는 이유는 한 회차가
        채널 전체의 며칠치 기록을 읽는 것을 막기 위해서다.
        """
        return min(
            max(self._settings.catchup_window_sec, outage_sec + 600),
            self._settings.catchup_max_window_sec,
        )

    def _catchup_retry_tick(self, worker: Worker) -> None:
        outage_sec = self.outage_tracker().check()
        if outage_sec is not None:
            # 닿지 않던 동안 들어온 요청은 소켓 이벤트로 다시 오지 않는다.
            # 돌아왔을 때 되짚지 않으면 그 시간의 요청은 영영 처리되지 않는다.
            log.info("슬랙 연결이 돌아왔다. 끊긴 시간 %.0f초", outage_sec)
            worker.catch_up(self.channel_ids(), window_sec=self._recovery_window_sec(outage_sec))

        for status in worker.retry_catchup():
            if not status.alert:
                continue
            # 오래 못 보면 사람이 알아야 한다. 조용히 다시 보기만 하면 몇
            # 시간째 안 잡히는 것을 아무도 모른다.
            self._notify_owner(
                "*되짚기를 오래 마치지 못하고 있습니다*\n\n"
                f"- 채널 : {status.channel}\n"
                f"- {status.stuck_sec / 60:.0f}분째입니다\n\n"
                "슬랙이 채널 기록을 계속 빈 목록으로 돌려줍니다.\n"
                "그 채널에서 답을 기다리는 요청이 있어도 잡히지 않습니다."
            )

    def ingress_services(self, restart: Callable[[str], None]) -> ServiceGroup:
        """접수 프로세스가 띄우는 주기 실행기 묶음.

        연결 점검은 소켓 연결이 이 프로세스에만 있으므로 여기서 안 띄우면
        어디서도 안 돈다. 명부 갱신도 접수가 맡는다 — 워커는 여럿 뜰 수 있어
        거기서 돌리면 같은 파일을 여러 프로세스가 동시에 쓴다.
        """
        return ServiceGroup(
            [
                self.health_runner(restart),
                self.roster_refresher(),
                self.attachment_cleanup_runner(),
                self.pending_report_runner(),
            ],
            name="ingress",
        )

    def worker_services(self, worker: Worker) -> ServiceGroup:
        """워커 프로세스가 띄우는 주기 실행기 묶음.

        되짚기 재시도는 그 워커의 큐에 넣으므로 워커를 함께 받는다.
        """
        return ServiceGroup(
            [
                self.state_snapshot_runner(),
                self.watch_runner(),
                self.job_purge_runner(),
                self.catchup_retry_runner(worker),
                self.pending_report_runner(),
            ],
            name="worker",
        )

    def self_restarter(self, exit_process: Callable[[int], None] | None = None) -> SelfRestarter:
        """기본 재기동 동작. 소유자에게 사유를 알리고 스스로 나간다.

        소유자 개인 대화가 설정돼 있을 때만 알린다. 없으면 알릴 곳이 없으므로
        사유는 로그에만 남기고 종료만 한다.
        """
        return SelfRestarter(
            notify=self._notify_owner if self._profile.owner_dm else None,
            inflight_count=lambda: self.inflight.count,
            grace_sec=self._settings.shutdown_grace_sec,
            exit_process=exit_process,
            on_shutdown_start=self.mark_shutting_down,
        )

    def _slack_reachable(self) -> bool:
        try:
            self._client.auth_test()
            return True
        except Exception:  # noqa: BLE001 — 슬랙 연결 확인 실패를 판정에 그대로 반영한다 — 어떤 예외든 연결 불가로 본다
            return False

    # -- 보조 -----------------------------------------------------

    @property
    def identity(self) -> BotIdentity:
        """이 봇의 신원. 자기 말 판정이 필요한 부품 전부가 이것을 받는다.

        부품마다 따로 만들면 같은 API 를 그 수만큼 부르고, 그중 하나가
        실패하면 그 부품만 다른 판정을 한다.
        """
        if self._identity is None:
            self._identity = SlackBotIdentity(
                self._client,
                clock=self._clock,
                retry_interval_sec=self._settings.identity_retry_interval_sec,
            )
        return self._identity

    def _is_self_message(self, msg: Any) -> bool:
        """이 봇이 올린 말인지 판정한다. 근거는 `identity` 하나뿐이다."""
        return self.identity.is_self(msg)

    def close(self) -> None:
        """DB 연결을 닫는다. 두 번 불러도 문제가 없다."""
        if self._closed:
            return
        self._closed = True
        if self._roster_refresher is not None:
            # 정지시키지 않으면 데몬 스레드가 프로세스 종료까지 슬랙을 계속 호출한다.
            self._roster_refresher.stop()
        if self._connection_watch is not None:
            # 떼지 않으면 프로세스가 여럿 뜨고 지는 동안 로거에 핸들러가 쌓여,
            # 같은 로그를 여러 감시가 중복으로 센다.
            for name in SOCKET_LOGGERS:
                logging.getLogger(name).removeHandler(self._connection_watch)
        self._database.close()


def _bot_token() -> str:
    import os

    return os.environ.get("SLACK_BOT_TOKEN", "")
