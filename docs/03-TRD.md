# slack-cli-agent · TRD

기술 설계. 어떻게 만드는가.

전제 — `02-PRD.md` 의 요구사항을 구현하는 구조다. 원본의 기술 부채(7,199줄 단일
파일, 전역 상태, import 불가능한 테스트)를 해소하는 것이 이 문서의 목적이다.

**설계 방침은 객체지향이다.** 모듈 수준 전역과 함수 집합이 아니라, 책임을 가진
객체와 그 객체 사이의 협력으로 구성한다.

---

## 0. 범위 — 구조 변경과 실행 구조 3가지

2026-09-14 사용자 결정이다. 이 제약이 아래 모든 설계에 우선한다.

### 1차 목표에 포함하는 것

**코드 구조 재편** — 모듈 분해, 클래스화, 전역 제거, 의존 주입, import 부수효과
제거, 테스트 방식 전환, 회사 결합 제거와 설정 외부화.

**실행 구조 변경 3가지** —

| 항목 | 내용 |
|---|---|
| Ingress·Worker 프로세스 분리 | 이벤트 수신과 장기 실행을 다른 프로세스로 나눈다 |
| SQLite 영속 작업 큐 | 기계가 쓰는 상태만 DB 로. 사람이 편집하는 설정은 파일로 유지 |
| 단일 파이썬 관리 CLI | 셸 스크립트 6종을 대체한다 |

영속 큐를 도입하면 **워커 크래시 감지가 필요해진다.** 프로세스를 나누면 워커가
죽었을 때 `RUNNING` 상태로 남은 작업을 되돌릴 수단이 없다. heartbeat 갱신과 정체
작업 재진입을 최소 형태로 포함한다.

### 1차 목표에서 제외하는 것

**기술 선택은 현행 유지한다.**

    유지    Socket Mode, Python 3.11+,
            실측 상수 전부, 응답 동작 전부

**신규 기능을 넣지 않는다.** 검토에서 나온 나머지 개선안(레이턴시 예산 SLO,
소켓 ping-pong 기반 리셋)은 범위 밖이다. 안정화 뒤 별도 과제로 다룬다.

### Socket Mode 수신과 프로세스 분리의 양립

충돌하지 않는다. Ingress 가 `slack_sdk` 의 `SocketModeClient` 로 이벤트를 받아
큐에 기록하고 즉시 반환한다. Worker 는 소켓을 열지 않고 `WebClient` 로만
발신한다. 소켓은 수신 전용으로 남는다.

원본은 `slack_bolt.App` 과 `SocketModeHandler` 를 썼다. bolt 는 `slack_sdk` 의
Socket Mode 구현 위에 데코레이터 등록과 미들웨어를 얹은 래퍼인데, 여기서는
핸들러 등록을 `SlackGateway` 가 맡으므로 그 계층이 쓰이지 않는다. 의존을
하나 줄이려고 SDK 를 직접 쓴다 — 받는 이벤트와 ack 규약은 같다.

### 동작 변경이 생기는 부분

영속 큐가 원본의 복구 로직 일부를 대체한다. **동작은 같거나 더 낫다.**

    save_restart_dropped    큐가 대체한다. 제거
    save_inflight_dropped   큐가 대체한다. 제거
    replay_restart_dropped  큐의 QUEUED 작업 재개가 대신한다. 제거
    catch_up (캐치업)       유지한다. 소켓 끊김 동안 도착한 메시지는 큐로 못 막는다
    _busy_threads·_thread_queue   큐의 스레드 배타 락이 대신한다. 제거

그 외 응답 동작은 전부 원본과 같아야 한다. **기능이 1대1 로 대응되어야 shadow
대조가 성립한다** — 같은 입력에 같은 출력이 나오는지 비교하는 것이 이식 검증의
유일한 수단이다.

---

## 0-1. 코드 3분류

원본 코드를 셋으로 나눈다. 분류가 곧 작업 방식이다.

| 분류 | 처리 방식 | 검증 |
|---|---|---|
| **이식** | 함수 본문을 수정하지 않는다. 클래스로 감싸기만 한다. 주석과 실측 근거를 함께 옮긴다 | shadow 대조로 바이트 단위 일치 |
| **재구성** | 로직은 유지하고 전역 제거와 의존 주입만 적용한다 | 단위 테스트 + shadow 대조 |
| **재설계** | 새로 쓴다 | 단위 테스트 |

### 이식 대상

회사 결합이 없고 슬랙의 예외적 동작이 밀집한 부분이다. **새로 쓰지 않는다.**
원본에 1년간 누적된 대응이 전부 여기 있고, 문서로 옮겨지지 않는 것이 많다.

| 원본 | 옮길 위치 |
|---|---|
| `to_mrkdwn`, `tables_to_bullets` | `render/markdown.py` |
| `md_chunks`, `fit_chunk`, `chunk`, `split_for_blocks`, `merge_tiny` | `render/splitter.py` |
| `verify_chunks`, `safe_fallback`, `separate_tables`, `blocks_rejected` | `render/verifier.py` |
| `clean_markers`, `preview`, `split_context` | `render/blocks.py` |
| `slack_ts`, `read_history`, `wait_history_slot` | `slack/history.py` |
| `unanswered`, `already_handled`, `reactions_on` | `reliability/catchup.py` |
| `REACTION_WORD`, `REACTION_GAP`, `REACTION_ONLY`, `MUTTER_ONLY`, `ASKED_BACK`, `worth_answering`, `asked_back` | `slack/gate.py` |
| `WATCH_RE`, `PROMISE_WITHOUT_WATCH_RE`, `ELAPSED_LINE`, `ELAPSED_MODEL_LINE` | `guard/` 각 가드 |
| `fix_plain_mentions`, `guard_wrong_addressee`, `late_rewrite_lost_content` | `guard/mentions.py`, `guard/rewrite.py` |
| `time_breakdown`, `detect_retries`, `usage_limit_message` | `engine/` 와 `observability/` |
| `mcp_ready`, `SocketErrorWatch` | `preflight/checks.py`, `reliability/health.py` |

정규식은 특히 그대로 옮긴다. `REACTION_WORD` 의 대안에 `+` 를 붙이지 않는 이유,
`REACTION_MAX_LEN` 으로 상한을 두는 이유가 주석에 있고 그것이 역추적 폭발을
막는다.

### 재구성 대상

로직은 원본대로 두고 전역과 부수효과만 걷어낸다.

    권한 판정          is_trusted, full_authority, mechanism_open -> AccessPolicy
    모델·effort 선택   model_for, effort_for -> AccessPolicy 메서드
    프롬프트 조립      build_system_prompt -> PromptSection 목록
    프롬프트 로드      prompt_text -> PromptLibrary
    지식 적재          조건부 로드 -> KnowledgeLoader
    세션 관리          get_session, reset_session -> SessionManager
    맥락 복원          thread_transcript, with_history -> TranscriptBuilder
    엔진 실행          run_claude, run_codex -> ClaudeEngine, CodexEngine
    엔진 전환          engine_state 처리 -> EngineSwitcher
    요청 처리          handle_request -> RequestHandler
    리액션 후처리      3종 중복 코드 -> ReviewTask 기반 클래스
    중복 방지          전역 dict 4개 -> DeduplicationTracker
    관리 명령          handle_admin 분기 -> AdminCommand 목록
    상태 파일 접근     파일마다 복사된 코드 -> JsonStore

### 재설계 대상

원본에 대응물이 없거나, 회사 결합 제거와 실행 구조 변경을 위해 필요한 것이다.

    JobQueue           SQLite 영속 작업 큐. 신규
    IngressDaemon      이벤트 수신 전용 프로세스. 원본 핸들러에서 접수 부분만 분리
    WorkerDaemon       작업 실행 프로세스. 원본 handle_request 의 실행 부분
    WorkerHeartbeat    크래시 감지와 정체 작업 재진입. 신규
    ManagementCLI      셸 스크립트 6종 대체
    Profile 확장       엔진 블록 분리, 플러그인 목록
    BotPlugin          전용 기능 확장점
    Outcome            조회 실패와 부재의 구분 타입
    PreflightRunner    점검을 클래스로. 검사 내용 자체는 원본 check.py 이식
    SecretLeakCheck    민감 정보 커밋 방지. 신규

---

## 1. 설계 원칙

### P1. 책임 하나에 클래스 하나

원본은 `bot.py` 한 파일이 수신·판정·조립·실행·변환·발신·복구를 전부 진다.
각각을 클래스로 분리하고 생성자로 의존을 주입한다.

### P2. 전역 상태를 두지 않는다

원본의 `BOT_USER_ID`, `OWNER_DM`, `_busy_threads`, `_slots`, `_sock_errors` 는
모두 모듈 전역이다. 이것이 테스트가 import 하지 못하는 직접 원인이다.

모든 상태는 객체의 인스턴스 속성으로 둔다. 프로세스 하나에 봇 하나라는 전제를
코드에 박지 않는다.

### P3. import 에 부수 효과를 두지 않는다

원본은 import 시점에 환경변수를 읽고 파일 경로를 만들고 슬랙 App 을 생성한다.
그래서 테스트가 AST 파싱으로 함수만 떼어 낸다.

모듈을 import 하는 것만으로는 아무 일도 일어나지 않게 한다. 구성은 명시적
팩토리 호출에서만 일어난다.

### P4. 정책은 데이터, 판정은 객체

상수를 흩어 두지 않고 설정 객체에 모은다. 판정 로직은 그 설정을 받는 객체의
메서드로 둔다.

### P5. 확장은 상속이 아니라 조합과 등록

엔진과 플러그인은 추상 기반 클래스를 구현하고 레지스트리에 등록한다.
어댑터 본체는 구체 타입을 모른다.

### P6. 실패는 명시적으로

조회 실패와 부재를 다른 타입으로 돌려준다. `None` 하나로 둘을 겸하지 않는다.
프롬프트 파일 누락처럼 되돌릴 수 없는 것은 예외를 낸다.

### P7. 상수에 근거를 남긴다

실측으로 정해진 값에는 그 측정을 주석으로 남긴다. 원본의 가장 큰 자산이
이것이다.

---

## 2. 패키지 구조

    slack_cli_agent/
      __init__.py
      __main__.py                엔트리포인트
      cli.py                     관리 CLI — run-ingress, run-worker, restart,
                                 check, apply, status, replay

      config/
        profile.py               Profile — 봇 하나의 정의
        channel.py               ChannelConfig, ChannelRegistry
        settings.py              RuntimeSettings — 타임아웃·상한 등 실측 상수
        paths.py                 StatePaths — 상태 디렉터리 하위 경로 계산

      core/
        application.py           Application — 최상위 조립과 수명 관리
        ingress.py               IngressDaemon — 수신 프로세스
        worker.py                WorkerDaemon — 실행 프로세스
        context.py               RequestContext — 요청 하나의 불변 맥락
        result.py                Outcome / Unknown — 실패와 부재의 구분
        errors.py                예외 계층

      slack/
        gateway.py               SlackGateway — Socket Mode 연결과 이벤트 분배
        listener.py              EventListener — 이벤트를 RequestContext 로 변환
        gate.py                  ResponseGate — 답할지 말지 판정
        reactions.py             ReactionMarker — 상태 표식 관리
        publisher.py             MessagePublisher — 발신과 실패 처리
        history.py               HistoryReader — 기록 조회. 빈 응답 재시도
        transcript.py            TranscriptBuilder — 대화 복원
        attachments.py           AttachmentStore

      auth/
        principal.py             Principal — 요청자의 신원
        policy.py                AccessPolicy — 계층 판정
        tools.py                 ToolPolicy — 허용 도구 결정

      prompt/
        library.py               PromptLibrary — 프롬프트 파일 로드와 자리표
        knowledge.py             KnowledgeLoader — 조건부 지식 적재
        composer.py              SystemPromptComposer — 조립 순서
        sections.py              PromptSection — 조각 하나의 추상

      engine/
        base.py                  Engine(ABC), EngineResponse, UsageLimit
        registry.py              EngineRegistry
        claude.py                ClaudeEngine
        codex.py                 CodexEngine
        switcher.py              EngineSwitcher — 전환 상태 기계
        runner.py                EngineRunner — 폴백을 포함한 실행

      storage/
        schema.py                DDL 과 마이그레이션 단계 목록
        database.py              Database — 커넥션과 트랜잭션
        repository.py            SqliteRepository — 저장소 구현의 공통 기반

      jobs/
        ports.py                 JobQueue(Protocol), Job, ReclaimResult — 계약
        queue.py                 SqliteJobQueue — 구현
        heartbeat.py             WorkerHeartbeat — 정체 판정 정책

      session/
        store.py                 SessionStore — 영속화
        manager.py               SessionManager — 스코프·TTL·복원

      render/                  [이식] 원본 함수 본문을 수정하지 않는다
        markdown.py              MarkdownConverter — mrkdwn 변환
        splitter.py              ContentSplitter — 안전 분할
        verifier.py              SplitVerifier — 분할 검증
        blocks.py                BlockBuilder — Block Kit 조립

      guard/
        base.py                  OutputGuard(ABC)
        mentions.py              PlainMentionGuard, AddresseeGuard
        watch.py                 WatchPromiseGuard
        rewrite.py               RewriteLossGuard
        pipeline.py              GuardPipeline

      reliability/
        catchup.py               CatchupService — 캐치업
        health.py                HealthMonitor — 연결 감시와 자가 재기동
        watchjobs.py             WatchJobQueue
        dedup.py                 DeduplicationTracker — 중복 처리 방어

      review/
        base.py                  ReviewTask(ABC) — 리액션 후처리의 공통 구조
        postmortem.py            PostmortemTask
        trace.py                 DebugTraceTask
        format.py                FormatReviewTask

      admin/
        command.py               AdminCommand(ABC)
        router.py                AdminRouter

      plugin/
        base.py                  BotPlugin(ABC) — 봇별 확장점
        loader.py                PluginLoader

      observability/
        audit.py                 AuditLog
        progress.py              ProgressStream
        notices.py               NoticeCatalog — 상태 안내문 목록

      preflight/
        check.py                 PreflightCheck(ABC)
        checks.py                구체 점검 4종
        runner.py                PreflightRunner

---

## 3. 핵심 타입

### 3.1 Profile — 봇 하나의 정의

```python
@dataclass(frozen=True)
class EngineSpec:
    type: str
    binary: Path
    model: str
    model_owner: str = ""
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Profile:
    name: str
    display_name: str
    primary_engine: EngineSpec
    fallback_engine: EngineSpec | None
    state_dir: Path
    work_root: Path
    data_dir: Path
    attach_dir: Path
    launch_label: str
    owner_user_id: str
    troubleshoot_channel: str
    owner_dm: str = ""
    plugins: tuple[str, ...] = ()

    @classmethod
    def load(cls, name: str, search_paths: Sequence[Path]) -> "Profile": ...

    def validate(self) -> list[str]:
        """설정 오류 목록. 빈 목록이면 정상."""
```

`frozen=True` 를 유지한다. 프로필은 기동 후 바뀌지 않는다.

**원본과 다른 점** — 봇의 정체성과 실행 엔진을 분리한다. 원본은 `engine`,
`engine_bin`, `model`, `model_owner` 가 최상위에 있고 `codex` 블록이 따로 있어,
엔진이 늘면 최상위 키가 그만큼 증가한다. `EngineSpec` 으로 묶으면 엔진 추가가
설정 형식을 바꾸지 않는다.

```json
{
  "name": "example",
  "display_name": "예시봇",
  "primary_engine": {
    "type": "claude",
    "binary": "~/.local/bin/claude",
    "model": "claude-sonnet-5",
    "model_owner": "claude-opus-5"
  },
  "fallback_engine": {
    "type": "codex",
    "binary": "codex",
    "model": "gpt-5.6-sol",
    "options": {"sandbox": "read-only"}
  }
}
```

### 3.2 RequestContext — 요청 하나의 불변 맥락

```python
@dataclass(frozen=True)
class RequestContext:
    channel: str
    user: str
    ts: str
    thread_ts: str
    text: str
    files: tuple[Mapping, ...]
    unaddressed: bool          # 멘션 없이 들어온 것
    late: bool                 # 캐치업으로 다시 집은 것
    requeued: bool             # 대기줄에서 다시 꺼낸 것
    queued_at: float | None
    first_reaction_at: float | None
```

원본은 슬랙 이벤트 dict 에 `_unaddressed`, `_late`, `_requeued`, `_queued_at`
같은 키를 추가로 심어 돌린다. 어떤 키가 언제 붙는지가 코드 전체에 흩어져 있다.
타입으로 고정한다.

### 3.3 Outcome — 판정 불가와 부재의 구분

```python
class Outcome(Generic[T]):
    """조회 결과. 성공 / 부재 / 판정 불가를 구분한다."""

    @classmethod
    def found(cls, value: T) -> "Outcome[T]": ...
    @classmethod
    def absent(cls) -> "Outcome[T]": ...
    @classmethod
    def unknown(cls, reason: str) -> "Outcome[T]": ...

    @property
    def is_unknown(self) -> bool: ...
```

원본의 `read_history` 는 빈 목록과 `None` 으로 이 둘을 구분하는데, 호출부가
그것을 안 지키면 조용히 "없다" 가 된다. 타입으로 강제한다.

### 3.4 Engine — 엔진 인터페이스

```python
class Engine(ABC):
    name: ClassVar[str]

    def __init__(self, profile: Profile, settings: RuntimeSettings): ...

    @abstractmethod
    def build_command(self, request: EngineRequest) -> list[str]:
        """실행할 명령줄."""

    @abstractmethod
    def parse(self, stdout: str, stderr: str, returncode: int) -> EngineResponse:
        """실행 결과를 공통 형식으로."""

    @abstractmethod
    def new_session_id(self) -> str:
        """이 엔진의 세션 식별자 발급 방식."""

    def session_id_from(self, response: EngineResponse) -> str | None:
        """엔진이 자기 세션 ID 를 발급하면 그것을 돌려준다. 기본은 None."""
        return None

    @abstractmethod
    def detect_usage_limit(self, response: EngineResponse) -> UsageLimit | None:
        """한도 소진 판정."""

    def readable_paths_note(self, paths: Sequence[Path]) -> str:
        """읽기 허용 경로를 알리는 방식. 인자로 되는 엔진은 빈 문자열."""
        return ""
```

```python
@dataclass(frozen=True)
class EngineRequest:
    prompt: str
    system_prompt: str
    session_id: str
    resume: bool
    model: str
    effort: str
    workdir: Path
    readable_dirs: tuple[Path, ...]
    tools: ToolSelection    # 허용 없음·허용목록·전면 금지 세 상태
    trust_level: TrustLevel


@dataclass(frozen=True)
class EngineResponse:
    ok: bool
    body: str
    session_id: str | None
    model_actual: str | None
    elapsed: float
    turns: int | None
    usage: Usage | None
    raw: Mapping[str, Any]
    failure_reason: str | None    # nonzero_exit / bad_json / timeout / usage_limit
```

**Claude 와 Codex 의 차이를 인터페이스가 흡수한다.**

    Claude   --add-dir 로 경로를 넘긴다      readable_paths_note 는 빈 문자열
             세션 ID 를 우리가 발급한다      session_id_from 은 None
             시스템 프롬프트가 턴마다 갱신   --append-system-prompt 로 매 턴 다시 싣는다

    Codex    경로 인자가 없다               readable_paths_note 가 문장을 만든다
             CLI 가 thread_id 를 발급한다    session_id_from 이 그것을 돌려준다
             최초 지시가 끝까지 우선한다     재개 턴은 그 턴 지침을 프롬프트 본문에 싣는다
                                            (engine/codex.py 의 _resume_prompt, sca-ivs)

### 3.5 EngineRegistry

```python
class EngineRegistry:
    def register(self, cls: type[Engine]) -> None: ...
    def create(self, name: str, profile: Profile,
               settings: RuntimeSettings) -> Engine: ...
    def available(self) -> list[str]: ...
```

엔진 모듈이 자기를 등록한다. 어댑터 본체는 `Engine` 만 안다. 세 번째 엔진
추가는 모듈 하나와 등록 한 줄이다.

### 3.5-1 FallbackEngine — 전환을 감싸는 데코레이터

```python
class FallbackEngine(Engine):
    """1차 엔진이 한도 소진을 내면 2차 엔진에 위임한다.

    호출부는 Engine 하나만 본다. 전환 여부를 몰라도 된다.
    """

    def __init__(self, primary: Engine, secondary: Engine,
                 switcher: EngineSwitcher, runner: EngineRunner): ...
```

`runner` 가 필요한 이유는 `build_command` 와 `parse` 가 순수 함수이기 때문이다.
전환 판정이 "1차를 실행해 보고 한도면 2차를 실행" 이므로 실행 수단을 들고 있어야
한다. subprocess 는 `EngineRunner` 가 맡고 테스트에서 주입으로 바꾼다.

원본은 `run_with_fallback` 이 호출부에서 전환 상태를 확인하고 분기한다.
데코레이터로 감싸면 전환 로직이 한 곳에 모이고 `RequestHandler` 는 그것을 모른다.

**동작은 원본 그대로다.** 전환은 즉시 하고, 사람이 승인하기 전에는 한도 안내만
답한다. `EngineSwitcher` 가 `engine_state.json` 과 `probe_engine` 을 담당한다.

### 3.6 AccessPolicy — 권한 계층

```python
class TrustLevel(IntEnum):
    GENERAL = 0
    TRUSTED = 1
    OWNER = 2


@dataclass(frozen=True)
class Principal:
    user_id: str
    channel: str
    trust: TrustLevel
    is_direct_message: bool


class AccessPolicy:
    def __init__(self, profile: Profile, channels: ChannelRegistry,
                 extensions: Sequence[AccessExtension] = ()): ...

    def principal_for(self, channel: str, user: str) -> Principal: ...
    def may_disclose_mechanism(self, principal: Principal) -> bool: ...
    def may_see_usage(self, principal: Principal) -> bool: ...
    def model_for(self, principal: Principal) -> str: ...
    def effort_for(self, principal: Principal, prompt: str) -> str: ...
```

원본의 판정 함수 넷(`is_trusted`, `full_authority`, `mechanism_open`,
`조직 전용 권한 판정`)이 서로 다른 인자를 받고 서로를 모르던 것을 하나로 모은다.
회사 전용 조건은 `AccessExtension` 으로 플러그인이 붙인다.

**소유자 하한 규칙을 `effort_for` 가 유지한다.** 채널값이 소유자 하한보다
낮으면 하한으로 올린다. 프롬프트에 `ultrathink` 가 있으면 올린다.

### 3.7 SystemPromptComposer

```python
class PromptSection(ABC):
    @abstractmethod
    def applies_to(self, ctx: CompositionContext) -> bool: ...

    @abstractmethod
    def render(self, ctx: CompositionContext) -> str: ...


class SystemPromptComposer:
    def __init__(self, library: PromptLibrary, knowledge: KnowledgeLoader,
                 sections: Sequence[PromptSection]): ...

    def compose(self, ctx: CompositionContext) -> str:
        """등록 순서대로 적용 가능한 조각만 이어붙인다."""
```

원본의 `build_system_prompt` 는 12단계 분기를 한 함수에 담고 있다. 각 단계를
`PromptSection` 으로 만들면 순서가 리스트로 드러나고, 플러그인이 자기 조각을
끼워 넣을 수 있다.

조각 목록(기본) —
`PersonaSection`, `KnowledgeSection`, `ChannelModeSection`, `OwnerNoteSection`,
`SlackFormatSection`, `ReviewFormatSection`, `AskerSection`, `TrustedSection`,
`SensitiveGuardSection`, `AuthoritySection`, `WatchSection`, `SilenceRuleSection`.

**PromptLibrary 의 실패 정책을 유지한다.**

```python
class PromptLibrary:
    def text(self, name: str, keep_slots: Collection[str] = ()) -> str:
        """파일이 없거나 비면 MissingPromptError. 가드가 빠진 채 답하지 않는다."""
```

### 3.8 GuardPipeline — 후처리

```python
class OutputGuard(ABC):
    name: ClassVar[str]

    @abstractmethod
    def apply(self, body: str, ctx: GuardContext) -> GuardResult: ...


@dataclass
class GuardResult:
    body: str
    changed: bool
    detail: Mapping[str, Any] = field(default_factory=dict)
    rerun: RerunRequest | None = None   # 모델에게 다시 쓰게 해야 하는 경우


class GuardPipeline:
    def __init__(self, guards: Sequence[OutputGuard], audit: AuditLog): ...

    def run(self, body: str, ctx: GuardContext) -> str:
        """순서대로 적용하고 바뀐 것을 감사 기록에 남긴다."""
```

원본의 보정 로직이 `handle_request` 안에 인라인으로 200줄 가까이 들어 있다.
가드마다 클래스로 떼면 테스트가 가드 단위로 가능해진다.

기본 가드 — `PlainMentionGuard`, `AddresseeGuard`, `WatchPromiseGuard`,
`RewriteLossGuard`, `NoticeCollisionGuard`.

`rerun` 이 있는 가드는 파이프라인이 엔진을 다시 부른다. `WatchPromiseGuard` 가
그 경우이고, 재시도에도 실패하면 해당 문장을 잘라내고 대체 문구를 붙인다.

### 3.9 RequestHandler — 처리 흐름

```python
class RequestHandler:
    def __init__(self, *, gateway, policy, sessions, composer, runner,
                 guards, publisher, marker, dedup, inflight, audit,
                 watch_queue, progress_factory, settings): ...

    def handle(self, ctx: RequestContext) -> None: ...
```

의존이 많다. 그것이 원본이 한 함수에 담고 있던 책임의 실제 수다. 생성자로
드러내면 무엇에 의존하는지가 보이고, 테스트에서 대역으로 바꿀 수 있다.

내부는 단계 메서드로 나눈다.

```python
def handle(self, ctx):
    if not self._admit(ctx):        return   # 재전송·권한·빈 본문·종료 중
    if self._handle_admin(ctx):     return
    with self._thread_slot(ctx) as slot:     # 대기줄·busy 관리
        if slot.queued:             return
        self._process(ctx)
```

`_thread_slot` 을 컨텍스트 매니저로 둔다. 원본의 `try/finally` 안에
대기줄 처리·표식 전환·다음 건 실행이 뒤섞여 있던 것을 분리한다.

### 3.10 신뢰성 객체

```python
class DeduplicationTracker:
    """중복 처리 방어 다층을 한 객체로 모은다."""
    def seen_event(self, channel: str, ts: str) -> bool: ...
    def mark_handled(self, channel: str, ts: str) -> None: ...
    def is_handled(self, channel: str, ts: str) -> bool: ...
    def mark_thread_consumed(self, thread_ts: str, up_to: float) -> None: ...
    def pick_next_queued(self, queued: Sequence[RequestContext],
                         thread_ts: str) -> tuple[RequestContext | None,
                                                  list[RequestContext]]: ...
```

원본의 `_seen_events`, `_handled_msgs`, `_thread_consumed`, `_busy_threads`,
`_thread_queue` 가 전부 모듈 전역 dict 이고 각자 lock 을 쓴다. 한 객체로 모아
lock 을 내부에 감춘다.

```python
class CatchupService:
    def __init__(self, history: HistoryReader, gate: ResponseGate,
                 notices: NoticeCatalog, dedup: DeduplicationTracker,
                 settings: RuntimeSettings): ...

    def find_missed(self, channel: str, window: float) -> Outcome[list[RequestContext]]: ...
    def sweep(self, channels: Iterable[str], window: float) -> CatchupReport: ...
    def retry_pending(self) -> None: ...
```

`Outcome` 을 돌려주므로 판정 불가를 호출부가 무시할 수 없다.

```python
class HealthMonitor:
    """연결 감시. logging.Handler 를 내부에 두고 소켓 로그를 센다."""
    def __init__(self, gateway, catchup, reporter, settings): ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
```

### 3.11 ReviewTask — 리액션 후처리 공통화

```python
class ReviewTask(ABC):
    emoji: ClassVar[str]
    log_name: ClassVar[str]

    @abstractmethod
    def build_prompt(self, target: ReviewTarget) -> str: ...

    @abstractmethod
    def build_header(self, target: ReviewTarget,
                     record: Mapping | None) -> str: ...

    def split_marker(self) -> str: ...

    def run(self, target: ReviewTarget) -> None:
        """공통 흐름 — 중복 확인, 기록, 실행, 분할 게시, 실패 시 기록 되돌리기."""
```

부검·디버그 추적·서식 점검은 구조가 거의 같고 프롬프트와 머리말만 다르다.
원본은 셋을 각각 200줄씩 복사해 뒀다. 기반 클래스로 묶으면 셋이 각
50줄 이하가 된다.

### 3.12 BotPlugin — 봇별 확장

```python
class BotPlugin(ABC):
    name: ClassVar[str]

    def prompt_sections(self) -> Sequence[PromptSection]: return ()
    def output_guards(self) -> Sequence[OutputGuard]: return ()
    def admin_commands(self) -> Sequence[AdminCommand]: return ()
    def access_extensions(self) -> Sequence[AccessExtension]: return ()
    def preflight_checks(self) -> Sequence[PreflightCheck]: return ()
```

프로필의 `plugins` 목록으로 로드한다. 원본 봇의 사내 워크플로 도구 조작이 이 형태로
옮겨간다 — 관리 명령, 프롬프트 조각, 권한 조건이 한 플러그인에 모인다.

---

## 4. 조립

```python
class Application:
    @classmethod
    def build(cls, profile_name: str) -> "Application":
        profile = Profile.load(profile_name, PROFILE_SEARCH_PATHS)
        settings = RuntimeSettings.from_profile(profile)
        ...
        return cls(...)

    def run(self) -> None:
        """기동 전 점검 -> 구성 -> 배경 작업 시작 -> 소켓 연결."""

    def shutdown(self, reason: str) -> None: ...
```

`__main__.py` 는 다음만 한다.

```python
def main() -> int:
    name = os.environ.get("BOT_PROFILE")
    app = Application.build(name)
    return app.run()
```

**import 부수 효과가 없다.** 테스트는 `Application.build` 를 부르지 않고
개별 객체를 직접 만든다.

---

## 5. 프로세스 구성과 동시성

### 두 프로세스

    [Slack Socket Mode]
            |
            v  WebSocket
    +---------------------------------------------+
    | IngressDaemon                               |
    |   Socket Mode 로 이벤트 수신                |
    |   중복 검사 (DeduplicationTracker)          |
    |   JobQueue 에 QUEUED 로 기록                |
    |   리액션 부여 (둘 중 하나만)                |
    |     eyes      바로 처리로 넘어간다          |
    |     hourglass 같은 스레드에 선행 건이 있다  |
    +----------------------+----------------------+
                           |  SQLite WAL
    +----------------------v----------------------+
    | WorkerDaemon                                |
    |   스레드 키 단위 배타 디큐                  |
    |   동시 실행 상한 (기본 10)                  |
    |   엔진 subprocess 실행                      |
    |   가드 적용, 서식 변환, 분할, 발신          |
    |   COMPLETED / FAILED 전이                   |
    +---------------------------------------------+

관리 명령은 Ingress 에서 즉시 처리한다. 모델을 거치지 않으므로 큐에 넣지 않는다.
원본과 같다.

### 작업 테이블

```sql
CREATE TABLE jobs (
  id            INTEGER PRIMARY KEY,
  channel       TEXT NOT NULL,
  thread_ts     TEXT NOT NULL,
  message_ts    TEXT NOT NULL,
  user_id       TEXT NOT NULL,
  payload       TEXT NOT NULL,          -- RequestContext 직렬화
  status        TEXT NOT NULL,          -- QUEUED / RUNNING / COMPLETED / FAILED
  worker_id     TEXT,
  heartbeat_ts  REAL,
  created_at    REAL NOT NULL,
  started_at    REAL,
  finished_at   REAL,
  attempts      INTEGER NOT NULL DEFAULT 0,
  UNIQUE(channel, message_ts)           -- 중복 등록 방지
);
CREATE INDEX idx_jobs_pick ON jobs(status, thread_ts, created_at);
```

`UNIQUE(channel, message_ts)` 가 중복 등록을 막는다. 원본의 `_seen_events` 와
`_handled_msgs` 가 하던 일을 제약 조건이 대신한다.

### 스레드 단위 순서 보장

```python
class JobQueue:
    def enqueue(self, ctx: RequestContext) -> bool:
        """이미 있으면 False. UNIQUE 위반을 중복으로 해석한다."""

    def claim_next(self, worker_id: str) -> Job | None:
        """실행 중인 작업이 없는 스레드에서 가장 오래된 QUEUED 하나를 잡는다.

        한 트랜잭션에서 조회와 상태 전이를 함께 한다.
        """

    def complete(self, job_id: int, ok: bool) -> None: ...
    def heartbeat(self, job_id: int) -> None: ...
```

`claim_next` 의 조건은 "그 `thread_ts` 에 `RUNNING` 이 없을 것" 이다. 선행 작업이
끝나야 다음이 뽑히므로 순서가 구조적으로 보장된다. 원본의 `_busy_threads` 와
`_thread_queue` 가 하던 일이고, 프로세스가 죽어도 유지된다.

**작업을 뽑을 때 모래시계를 떼고 눈을 단다.** 두 표식은 동시에 달리지 않는다.
접수 때 무조건 모래시계를 달면 모든 요청에 눈과 모래시계가 같이 떠서 대기
상태를 구분하지 못한다.

### 크래시 감지

워커가 실행 중 5초마다 `heartbeat_ts` 를 갱신한다. 15초 이상 갱신이 멈춘
`RUNNING` 작업은 `QUEUED` 로 되돌리고 `attempts` 를 올린다. 재시도 상한을 넘으면
`FAILED` 로 두고 소유자에게 알린다.

**되돌릴 때 리액션을 원래대로 맞춘다.** 눈 표식이 남아 있으면 모래시계로 바꾼다.

### 배경 작업

캐치업·건강 감시·지켜보기 큐·사용량 확인·인명표 갱신은 Worker 프로세스가
소유한다. Ingress 는 수신만 한다.

```python
class BackgroundTasks:
    def spawn(self, name: str, fn: Callable, interval: float) -> None: ...
    def stop_all(self, grace: float) -> None: ...
```

---

## 6. 상태 저장

**기계가 쓰는 상태는 DB, 사람이 편집하는 설정은 파일이다.** 이 구분이 기준이다.

파일이어야 하는 이유는 원본의 상태 외부화 원칙이다 — 프롬프트와 페르소나와 채널
설정은 요청마다 다시 읽어 재기동 없이 반영되고, 사람이 편집기로 고친다. DB 에
넣으면 그 두 가지를 잃는다.

### SQLite (`state.db`)

    jobs          작업 큐
    sessions      대화 단위와 session_id 매핑. 실행 환경(workdir·model)도 함께
    audit         감사 기록
    reviews       부검·디버그 추적·서식 점검 실행 기록
    watch_jobs    지켜보기 큐

원본은 세션만 SQLite 이고 나머지는 JSON 파일이라 파일마다 읽기·쓰기·오류 처리가
복사돼 있다. 기계가 쓰는 것을 한 DB 로 모으면 그 중복이 없어지고 트랜잭션으로
묶인다.

#### 스키마 변경 이력

    1   초기 스키마
    2   sessions 에 workdir·model 추가
    3   watch_jobs 에 msg_ts·checks·trust_level·extra 추가

실행 환경을 화자가 아니라 대화 단위로 정하고 한 번 넓어진 값을 좁히지 않는 것이
원본의 규칙인데, 1단계 스키마에 그것을 저장할 컬럼이 없었다. 2단계는 컬럼 추가뿐이고
기본값이 빈 문자열이라 이미 있는 행이 그대로 유효하다.

3단계는 감시 작업의 등록 시점 맥락이다. 확인은 등록보다 한참 뒤에 다른 프로세스에서
일어나므로, 그때 다시 구할 수 없는 값은 등록할 때 적어야 한다. `msg_ts` 가 없으면
완료 시 감시 표식 리액션을 뗄 대상을 지목할 수 없어 원본 동작을 재현하지 못한다.
`checks` 가 없으면 확인 상한을 둘 수 없다. `trust_level` 은 원본 `is_owner` 를
TrustLevel 로 일반화한 것이다. 원본 `org_admin` 처럼 조직 전용 값은 코어 컬럼으로
올리지 않고 `extra` 에 JSON 으로 넣는다.

확인 상한을 넘긴 건은 `due` 에서 빠지고 `expired` 에 나온다. 양쪽에서 다 빠지면 그
건은 아무 조회에도 안 나와 방치된다.

**이미 적용된 단계를 고치지 않는다.** 바꿀 것은 단계를 더한다 — 돌고 있는 DB 는
`user_version` 을 보고 모자란 단계만 적용한다.

```python
class Database:
    """커넥션 관리와 스키마 마이그레이션. WAL 모드."""
    def __init__(self, path: Path): ...
    def migrate(self) -> int: ...
    def transaction(self) -> ContextManager[sqlite3.Connection]: ...
```

### 저장소의 계약과 구현을 분리한다

**계약은 도메인 패키지의 Protocol 이 정의하고, 구현은 `SqliteRepository` 를
상속한다.** 호출부는 Protocol 에만 의존하므로 큐 호출이 실패하는 경우를 대역으로
시험할 수 있다.

ABC 가 아니라 Protocol 을 쓰는 이유는 저장소의 공통 구현이 SQLite 전용이기
때문이다. 커넥션 접근과 트랜잭션 진입을 계약에 넣으면 계약과 수단이 섞인다.
`Engine` 쪽은 공유 기본 구현이 있어 ABC 가 맞고 여기는 반대다.

**동시성 계약은 docstring 과 테스트가 드러낸다.** "같은 thread_ts 는 동시에
나오지 않는다", "여러 워커가 동시에 불러도 같은 작업을 두 번 내주지 않는다" 가
`JobQueue` Protocol 의 docstring 에 있고 같은 문장이 테스트 이름이다. SQL 이
보이는 것은 계약 표현이 아니다.

### 구현이 지켜야 할 세부

코드에서만 드러나고 위 타입 정의에는 안 보이는 것들이다. 재작성할 때 이것이
빠지면 동작이 달라진다.

**ChannelRegistry 는 mtime 이 바뀔 때만 다시 파싱한다.** 요청마다 `stat` 하나이고
파싱은 변경 시에만 한다. 파일이 없으면 빈 목록이다. **JSON 파싱에 실패하면 직전
설정을 유지한다** — 사람이 편집 중 저장한 깨진 파일로 전체 처리가 멈추면 안 된다.
mtime 이 또 바뀌므로 고치면 바로 반영된다.

**Profile 의 경로 기본값** — `state_dir` 이 없으면 `~/.<name>`, `work_root` 는
`<state_dir>/work`, `data_dir` 은 `<state_dir>/data`, `attach_dir` 은
`<state_dir>/attachments`, `launch_label` 은 `local.<name>`. 전부 `expanduser` 를
적용한다.

**RequestContext 는 JSON 왕복이 손실 없이 된다.** 큐의 `payload` 컬럼에 그대로
들어가므로, 직렬화한 뒤 복원한 값이 원본과 같아야 한다.

**Database 는 스레드마다 커넥션을 따로 연다.** sqlite3 커넥션은 스레드 간 공유가
안 된다. `isolation_level=None` 으로 두고 트랜잭션을 직접 연다 — 파이썬 드라이버의
암묵적 트랜잭션이 `BEGIN IMMEDIATE` 와 충돌한다.

**PRAGMA 설정** — `journal_mode=WAL`, `busy_timeout=30000`, `foreign_keys=ON`.

**Outcome 의 `value_or` 는 부재일 때만 기본값을 준다.** 판정 불가는 예외를 낸다.
이 구분이 이 타입의 존재 이유다.

### ORM 을 쓰지 않는다

**근거는 의존성 무게가 아니다.** 실측에서 SQLAlchemy 2.0.52 는 디스크 19MB 이고
`sqlalchemy.orm` import 가 110-127ms 다. 상시 실행 데몬이라 기동 때 한 번이고
요청 중에는 0이므로 부담이 아니다.

**쓰지 않는 이유는 이식성 이득이 없다는 것이다.** 이 설계의 동시성은
`BEGIN IMMEDIATE`·`busy_timeout`·WAL 이라는 SQLite 고유 동작에 의존한다. 다른 DB
로 옮길 계획이 없는데 DB 추상화를 도입하면 그 값을 쓰지 못한다.

**대신 마이그레이션을 직접 만든다.** ORM 을 쓰는 쪽의 실질적 이점이 Alembic 이라
그것을 대체하지 않고 빼면 안 된다. `MIGRATIONS` 에 (버전, 설명, 문장 목록)을
순서대로 두고, `user_version` 을 읽어 아직 적용 안 된 단계만 실행한다. 단계
하나가 한 트랜잭션이다.

`executescript` 를 쓰지 않는다 — 실행 직전에 열린 트랜잭션을 자동 커밋해서
마이그레이션 단계의 원자성이 깨진다. DDL 을 문장 단위로 나눠 `execute` 한다.

**스키마가 자주 바뀔 전망이면 이 판단을 다시 본다.** 그 경우 SQLAlchemy Core 와
Alembic 이 낫다. ORM 계층은 그때도 쓰지 않는다 — `claim_next` 의 트랜잭션 격리를
세션 flush 의미와 함께 따져야 해서 고려할 것이 늘어난다.

감사 기록은 `audit.jsonl` 을 함께 남긴다. 원본 도구와 대시보드가 그 형식을 읽고,
shadow 대조에도 쓴다.

### 파일

    prompts/*.md         프롬프트 18종
    persona/*.md         페르소나와 지식
    channels.json        채널 설정
    profile.json         봇 정의
    engine/*.json        엔진별 권한·MCP 설정
    engine_state.json    엔진 전환 상태

`engine_state.json` 은 기계가 쓰지만 사람이 직접 열어 확인하고 되돌리는 경우가
있어 파일로 둔다. 원본과 같다.

#### channels.json 의 코어 키

원본 bot.py 에서 실제로 조회되는 키를 실측해 정했다. 전부 `ChannelConfig` 의
정식 필드다.

    mode                응답 방식
    workdir             작업 디렉터리
    model               모델
    effort              추론 강도
    persona             페르소나 파일
    knowledge           지식 파일 목록
    trusted_users       이 채널에서 신뢰하는 사용자
    user_tools          사용자별 추가 허용 도구 표. 소유자에게는 안 따진다
    answer_unaddressed  호명 없는 메시지에도 답하는가. 원본 mention_only 의 반대다.
                        슬랙에서 「끼어들기 허용」 · 「멘션 전용」 으로 바꾼다
    name                슬랙에서 조회한 채널 이름. 없으면 채널 ID
    session_scope       대화를 잇는 단위. thread(기본) 또는 channel
    disclose_mechanism  구조 공개 허용
    skills              Skill 도구 개방
    light_context       맥락을 줄여 넘기는가
    rich                실행 모델 표기 등 상세 출력
    chat                채널 대화량. 기본 normal. 프롬프트 문구와 응답 판정 양쪽에 쓴다

#### 호명 없는 스레드 답글의 판정

`slack/policy.py` 의 `ResponsePolicy` 한 곳에서 정한다. 리스너는 사실만 모아
넘긴다 — 채널 설정과 스레드 상태(봇이 이미 말했는가, 그 말이 되물음이었는가).

    answer_unaddressed 꺼짐   안 받는다. 등록 안 된 채널도 같다
    다른 참가자 호명          안 받는다. chat 값과 되물음 여부보다 앞선다
    chat=quiet              봇이 되물은 답만 받는다
    chat=normal             봇이 낀 스레드에서 ResponseGate 를 통과한 말만
    chat=active             봇이 안 낀 스레드도 같은 기준으로 받는다

`considers` 는 채널 설정만 본다. 스레드 조회가 슬랙 API 호출이라 그 앞에서
거르려고 나눠 둔 것이다.

**「나를 안 불렀다」 와 「아무도 안 불렀다」 는 다르다.** 한 스레드에 봇이 여럿
있고, 선두 멘션이 다른 참가자를 가리키면 그 요청은 그 참가자 몫이다.
`addresses_someone_else` 가 이것을 판정하고 `ResponsePolicy.answers` 와 복구
경로(`reliability/catchup.py`)가 함께 쓴다. 한쪽만 적용하면 실시간으로 거른
메시지를 복구가 다시 집어 든다.

판정은 선두 위치만 본다. 문장 안의 멘션("아까 `<@U1>` 가 말한 것 확인해줘")은
호명이 아니라 지칭이라, 그것까지 거르면 실제 요청이 사라진다.

**여기 없는 키는 `extra` 에 보존되고 플러그인이 읽는다.** 원본의 `org_admins`
가 그 예다 — 조직 전용 판정이라 코어 필드로 올리지 않는다.

**정식 필드를 `extra` 로 조회하지 않는다.** 문자열 키로 읽으면 오타를 타입 검사로
검출하지 못한다. 코어가 읽는 키는 필드로 올린다.

**쓰기는 임시 파일에 쓰고 교체한다.** 원본은 대상 파일에 바로 써서, 쓰는 동안
읽으면 잘린 JSON 이 읽힌다. 읽는 쪽은 그것을 편집 중 파일로 보고 직전 설정을
유지하므로 바뀐 값이 한 주기 늦게 반영된다. 교체 방식은 읽는 쪽이 옛 파일이나
새 파일 하나만 보게 한다.

**쓴 뒤 캐시를 직접 갱신한다.** mtime 해상도가 1초인 파일 시스템에서 같은 초에
두 번 쓰면 mtime 이 안 바뀌어 캐시가 유지되고 첫 번째 값이 읽힌다.

**알 수 없는 키는 쓰기가 보존한다.** 플러그인이 쓰는 항목을 코어의 쓰기가 지우면
안 된다.

```python
class JsonStore(Generic[T]):
    """JSON 파일 하나를 다루는 공통 계층. 읽기 실패를 기본값으로 흡수한다."""
    def __init__(self, path: Path, default: Callable[[], T]): ...
    def load(self) -> T: ...
    def save(self, value: T) -> None: ...
    def mutate(self) -> ContextManager[T]:
        """lock 안에서 읽고 고치고 쓴다."""
```

---

## 7. 설정 파일 배치

    저장소
      profiles/example.json          예시. 커밋한다
      prompts/*.md.example           예시 프롬프트. 커밋한다
      .gitignore                     profiles/*.json 을 example 빼고 제외

    상태 디렉터리 (~/.<bot-name>/)
      profile.json                   실제 프로필. 커밋 대상 아님
      channels.json                  채널 설정
      prompts/*.md                   프롬프트 본문
      persona/*.md                   페르소나와 지식
      engine/settings-*.json         엔진별 권한 설정
      engine/mcp.json                MCP 설정
      engine_state.json              엔진 전환 상태
      state.db                       기계 상태 — 큐·세션·감사·부검·지켜보기
      audit.jsonl                    감사 기록 사본. 외부 도구가 읽는다

`.gitignore` 에 넣을 것 —

    profiles/*.json
    !profiles/*.example.json
    prompts/*.md
    !prompts/*.md.example
    persona/
    *.local.json

**커밋 전 검사를 preflight 에 넣는다.** 채널 ID 형식(`C`/`D` + 영숫자 10자
이상), 사용자 ID 형식(`U` + 영숫자), 절대 홈 경로를 저장소 파일에서 grep 해
검출한다.

---

## 8. 기동 전 점검

```python
class PreflightCheck(ABC):
    name: ClassVar[str]

    @abstractmethod
    def run(self, profile: Profile) -> CheckResult: ...


class PreflightRunner:
    def run_all(self) -> list[CheckResult]:
        """하나라도 실패하면 기동하지 않는다."""
```

기본 점검 —

1. `UndefinedNameCheck` — AST 로 정의되지 않은 이름 참조 검출
2. `McpServerCheck` — 실행 파일·권한·셰뱅 인터프리터가 PATH 에 있는지.
   launchd 의 PATH 가 좁아 `#!/usr/bin/env node` 가 조용히 실패하는 것을 잡는다
3. `PromptFileCheck` — 소스에서 `library.text("NAME")` 호출을 뽑아 목록을 손으로
   관리하지 않는다
4. `WorkdirCheck` — 존재, 홈 밖, `CLAUDE.md` 없음
5. `SecretLeakCheck` — 저장소 파일의 민감 패턴 검출

플러그인이 자기 점검을 추가한다.

`restart.sh` 는 `py_compile -> preflight -> pytest` 를 돌리고 실패하면 재시작을
중단해 기존 프로세스를 유지한다. PID 파일로 대상을 고른다.

---

## 9. 테스트 전략

원본이 AST 파싱을 쓴 이유는 import 가 안 되기 때문이고, 그래서 테스트가
프롬프트 문자열 검증에 머물렀다. 구조를 바꾸면 정상 테스트가 가능하다.

### 단위

각 클래스를 대역 의존으로 만들어 테스트한다.

    AccessPolicy       계층 판정과 소유자 하한 규칙
    ResponseGate       맞장구·혼잣말·되물음 판정
    MarkdownConverter  변환 규칙
    ContentSplitter    표·코드블록·인용을 자르지 않는지
    SplitVerifier      분할 검증이 실제로 실패를 잡는지
    각 OutputGuard     보정 전후
    Engine 구현        명령 조립과 파싱. 실제 실행 없이
    CatchupService     빈 응답을 부재로 판정하지 않는지, 짝짓기가 맞는지

### 통합

가짜 슬랙 클라이언트와 가짜 엔진으로 `RequestHandler` 전 흐름을 돌린다.

시나리오 —

- 처리 중 종료 신호 -> 재기동 -> 그 요청이 한 번만 다시 처리된다
- 같은 스레드 동시 요청 -> 순서대로 하나씩
- 발송 직전 스레드가 이어짐 -> 반영해 한 번만 답한다
- 엔진 한도 소진 -> 전환 후 승인 전에는 안내만
- Block Kit 거절 -> 평문으로 낮춰 발신

### 격리

**시험이 시험 밖 상태에 의존하지 않게 구조로 막는다.** 실제 상태 디렉터리와
실제 시계를 쓰지 못하게 한다.

    Clock 을 주입한다. time.time() 을 직접 부르지 않는다
    StatePaths 를 주입한다. 실제 홈 경로를 반환하는 경로가 시험에 없다
    conftest.py 에서 sys.audit 훅으로 운영 파일 열기를 예외로 만든다

격리를 넣었으면 실제로 막히는지 실행으로 확인한다 — 전체 시험 후 운영 파일
mtime 이 바뀌지 않은 것을 본다.

### shadow 대조

**이식 분류 코드가 제대로 옮겨졌는지는 이것으로만 확인된다.** 단위 테스트는 내가
예상한 입력만 검증하므로, 원본에 있으나 문서로 못 옮긴 동작은 검출되지 않는다.

방식은 다음과 같다.

1. 원본 `audit.jsonl` 에서 실제 요청과 응답 쌍을 꺼낸다
2. 같은 응답 본문을 범용판의 `render` 계열에 넣어 분할·변환 결과를 만든다
3. 원본이 실제로 발신한 조각과 바이트 단위로 대조한다

운영 전환 단계에서는 범용판이 같은 이벤트를 받아 처리하되 발신하지 않고, 원본 봇가
낸 답과 비교한다. 엔진 응답은 같지 않으므로 대조 대상은 **서식·분할·가드 적용
결과**다. 같은 본문을 넣었을 때 같은 조각이 나오는지를 본다.

### 회귀

상태 안내문이 목록에서 빠지는 것을 검출한다. `publisher.post` 로 나가는
리터럴을 AST 로 훑어 `NoticeCatalog` 에 없는 것을 실패로 만든다. 원본의
이 테스트는 유지할 가치가 있다.

---

## 10. 실측 상수 이관

`RuntimeSettings` 에 모으되 근거 주석을 그대로 옮긴다.

```python
@dataclass(frozen=True)
class RuntimeSettings:
    # 300초는 150건 중 2건을 잘랐고 중앙값은 34초였다
    request_timeout_sec: float = 900
    slow_report_sec: float = 800
    # 벽시계와 monotonic 의 차이. 43분 보고가 실제 42초였던 사례
    sleep_gap_suspect_sec: float = 30
    max_concurrent: int = 10
    # 명부에 넣을 계정 핸들의 형태. 빈 문자열이면 형식을 안 본다
    roster_handle_pattern: str = r"\."
    slack_chunk: int = 3500
    markdown_block_limit: int = 12000
    session_ttl_hours: int = 24
    channel_session_ttl_days: int = 7
    catchup_window_sec: float = 7200
    catchup_max_window_sec: float = 86400
    catchup_thread_lookback_sec: float = 7 * 86400
    # 슬랙 읽기 지연만 덮는 값. 3600 은 틀린 값이었다
    catchup_grace_sec: float = 120
    # 너무 빨리 부르면 429 대신 ok + 빈 목록이 온다
    history_min_interval_sec: float = 2.0
    history_read_tries: int = 3
    history_read_pause_sec: float = 2.0
    # 정상 4시간35분에 0회, 장애 22분에 128회
    socket_reconnect_limit: int = 4
    # 이전 20건/180초는 실제 발생률 16.8건보다 높아 한 번도 발화하지 않았다
    socket_error_limit: int = 8
    health_interval_sec: float = 30
    shutdown_grace_sec: float = 330
    watch_check_interval_sec: float = 300
    watch_job_min_gap_sec: float = 300
    watch_job_max_age_sec: float = 24 * 3600
    history_max_msgs: int = 40
    history_max_chars: int = 12000
    linked_thread_max: int = 3
    late_rewrite_min_ratio: float = 0.6
    late_rewrite_min_chars: int = 200
    progress_tick_sec: float = 3
    progress_idle_sec: float = 45
    # 추측한 값으로 퍼센트를 만들지 않는다. 비워 둔다
    context_limit: Mapping[str, int] = field(default_factory=dict)
```

프로필에서 개별 항목을 덮어쓸 수 있게 한다. 기본값은 실측이고, 다른 환경에서는
다를 수 있다.

---

## 11. 슬랙 API 주의사항

원본에서 실측으로 확인된 것들이다. 코드에 방어를 남긴다.

| 사항 | 처리 |
|---|---|
| `ok` 와 함께 빈 `messages` 를 준다 | 세 번 읽고 그래도 비면 `Outcome.unknown` |
| 타임스탬프 소수 7자리를 `oldest` 로 주면 빈 목록 | `f"{float(v):.6f}"` 로 고정 |
| `invalid_blocks` 로 리치 표기를 거절 | 평문으로 낮춰 재발신 |
| 마크다운 블록이 빈 `text` 를 거절 | 본문이 비면 블록을 넣지 않는다 |
| 표와 문단이 붙으면 문단을 표 행으로 렌더 | 분할 전후 두 번 띄운다 |
| 권한 없는 파일 다운로드가 200 + HTML | `Content-Type` 을 확인한다 |
| `users.list` 의 `name` 이 계정 핸들 | 이메일 스코프 없이 실명과 잇는다 |

---

## 12. 원본 대비 변경 요약

| 항목 | 원본 | 범용판 |
|---|---|---|
| 구조 | 단일 파일 7,199줄 | 패키지. 모듈당 책임 하나 |
| 상태 | 모듈 전역 | 인스턴스 속성. 생성자 주입 |
| import | 부수 효과 있음 | 없음 |
| 테스트 | AST 로 함수 추출 | 정상 import |
| 엔진 | 분기문으로 갈림 | ABC + 레지스트리 |
| 권한 | 판정 함수 4개가 따로 | `AccessPolicy` 하나 |
| 프롬프트 조립 | 12단계 분기 한 함수 | `PromptSection` 목록 |
| 후처리 가드 | 인라인 200줄 | `OutputGuard` 파이프라인 |
| 리액션 후처리 | 3종 각 200줄 복사 | `ReviewTask` 기반 클래스 |
| 중복 방지 | 전역 dict 4개 | `DeduplicationTracker` |
| JSON 상태 | 파일마다 코드 복사 | `JsonStore` 공통 |
| 조회 실패 | `None` 과 빈 목록 | `Outcome` 타입 |
| 조직 결합 | 코드에 하드코딩 | 프로필과 플러그인 |
| 전용 기능 | 본체에 섞임 | `BotPlugin` |
| 프로세스 | 단일 프로세스 | Ingress·Worker 분리 |
| 작업 큐 | 인메모리 + 파일 보존 | SQLite 영속 큐 |
| 재기동 복구 | 복구 로직 1,000줄 | 큐의 QUEUED 재개 |
| 크래시 감지 | 없음 | heartbeat 와 정체 작업 재진입 |
| 기계 상태 | JSON 파일 6종 + SQLite | SQLite 하나 |
| 사람 편집 설정 | 파일 | 파일 유지 |
| 운영 명령 | 셸 스크립트 6종 | 파이썬 CLI 하나 |

---

## 13. 구현 순서

PRD 9절의 단계에 대응한다. 각 단계 끝에 동작하는 봇이 있어야 한다.

**각 단계 안에서는 테스트 먼저, 그 다음 이식, 그 다음 재구성 순이다.** 이식
대상은 shadow 대조로 검증한 뒤에 그것을 쓰는 재구성 코드를 작성한다. 순서를
뒤집으면 불일치가 났을 때 이식이 틀린 것인지 그것을 쓰는 코드가 틀린 것인지
구분하지 못한다.

**3단계까지는 Ingress 와 Worker 를 한 프로세스에서 돌려도 된다.** 큐를 사이에
두는 구조만 1단계부터 지키면 프로세스 분리는 기동 방식 변경으로 끝난다.

### 1단계 — 골격

    config/        Profile, RuntimeSettings, StatePaths, ChannelConfig
    core/          Application, RequestContext, Outcome
    engine/        Engine, EngineRegistry, ClaudeEngine
    slack/         SlackGateway, EventListener, MessagePublisher
    render/        MarkdownConverter, ContentSplitter
    prompt/        PromptLibrary, SystemPromptComposer (최소 조각)
    preflight/     PromptFileCheck, WorkdirCheck

### 2단계 — 정책

    auth/          Principal, AccessPolicy, ToolPolicy
    session/       SessionStore, SessionManager, TranscriptBuilder
    slack/         ResponseGate, ReactionMarker, HistoryReader
    guard/         GuardPipeline + 기본 가드 4종
    admin/         AdminRouter + 기본 명령
    observability/ AuditLog, NoticeCatalog

### 3단계 — 신뢰성

    jobs/          Database, JobQueue, WorkerHeartbeat
    core/          IngressDaemon, WorkerDaemon
    reliability/   DeduplicationTracker, CatchupService, HealthMonitor,
                   WatchJobQueue
    cli.py         run-ingress, run-worker, restart, check, apply, status, replay

### 4단계 — 확장

    engine/        CodexEngine, EngineSwitcher, EngineRunner
    plugin/        BotPlugin, PluginLoader
    review/        ReviewTask + 3종
    observability/ ProgressStream
    render/        BlockBuilder, SplitVerifier

---

## 14. 원본 봇 전환 계획

범용판은 최종적으로 원본 봇를 대체한다. 한 번에 전환하지 않는다.

### 전제

- **원본 봇 저장소를 수정하지 않는다.** 운영 중인 봇이라 순수 로직 추출 리팩토링을
  먼저 하면 그 자체가 회귀 위험이다. 원본은 읽기 전용으로만 참조하고, 이식은
  함수 본문을 그대로 복사해 오는 것으로 한다. 대신 shadow 대조로 검증한다
- 두 봇이 같은 워크스페이스에서 동시에 운영된다. 슬랙 앱을 따로 만든다
- 상태 디렉터리·작업 디렉터리·PID 파일·launchd 라벨이 갈린다
- 원본이 이미 원본 봇와 구치파치를 같은 코드로 운영하고 있어 다중 봇 공존은
  검증된 구조다

### 순서

1. **테스트 먼저 작성** — 11절 슬랙 API 주의사항 표가 그대로 테스트 목록이다.
   구현보다 먼저 쓴다
2. **이식 분류 구현과 shadow 대조** — 원본 `audit.jsonl` 로 서식·분할 결과를
   바이트 단위 대조한다. 불일치가 0이 될 때까지 다음 단계로 가지 않는다
3. **재구성·재설계 구현**
4. **회사 전용 기능을 플러그인으로 이전** — 사내 워크플로 도구 조작, 사내 API 헬프데스크
   모드, 조직 코치 모드
5. **채널 1개로 운영 시작** — 트러블슈팅 채널처럼 영향이 작은 곳부터
6. **회귀 확인 후 담당 채널 확대**
7. **원본 봇 중지**

### 전환 판정 기준

각 채널을 옮기기 전에 확인한다.

    엣지 케이스 테스트 전부 통과
    shadow 대조 불일치 0
    그 채널에서 며칠 운영해 리액션 상태가 원본과 같게 전이되는지 확인
    캐치업이 같은 요청을 중복 처리하지 않는지 확인

### 되돌리기

채널 설정에서 범용판을 빼고 원본 봇에 다시 등록한다. 상태 디렉터리가 갈려 있어
서로 영향이 없다. 세션은 끊기지만 슬랙 대화에서 맥락을 다시 세우므로 대화가
처음부터 시작되지는 않는다.
