"""스키마 정의와 마이그레이션 단계.

버전마다 단계를 하나씩 추가한다. 기존 DB 는 `user_version` 다음 단계부터
순서대로 적용한다. 초기 생성과 버전 업이 같은 경로를 쓰므로 새 DB 와 운영 중
DB 가 같은 스키마로 수렴한다.
"""

from __future__ import annotations

V1_INITIAL_SQL = """
CREATE TABLE jobs (
  id            INTEGER PRIMARY KEY,
  channel       TEXT    NOT NULL,
  thread_ts     TEXT    NOT NULL,
  message_ts    TEXT    NOT NULL,
  user_id       TEXT    NOT NULL,
  payload       TEXT    NOT NULL,
  status        TEXT    NOT NULL,
  worker_id     TEXT,
  heartbeat_ts  REAL,
  created_at    REAL    NOT NULL,
  started_at    REAL,
  finished_at   REAL,
  attempts      INTEGER NOT NULL DEFAULT 0,
  failure       TEXT,
  UNIQUE(channel, message_ts)
);
CREATE INDEX idx_jobs_pick ON jobs(status, thread_ts, created_at);
CREATE INDEX idx_jobs_heartbeat ON jobs(status, heartbeat_ts);

CREATE TABLE sessions (
  scope        TEXT NOT NULL,
  key          TEXT NOT NULL,
  session_id   TEXT NOT NULL,
  engine       TEXT NOT NULL,
  created_at   REAL NOT NULL,
  last_seen_ts TEXT NOT NULL DEFAULT '',
  updated_at   REAL NOT NULL,
  PRIMARY KEY (scope, key)
);

CREATE TABLE audit (
  id        INTEGER PRIMARY KEY,
  at        REAL NOT NULL,
  kind      TEXT NOT NULL,
  channel   TEXT NOT NULL DEFAULT '',
  thread_ts TEXT NOT NULL DEFAULT '',
  payload   TEXT NOT NULL
);
CREATE INDEX idx_audit_at ON audit(at);

CREATE TABLE reviews (
  kind      TEXT NOT NULL,
  channel   TEXT NOT NULL,
  target_ts TEXT NOT NULL,
  at        REAL NOT NULL,
  result    TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (kind, channel, target_ts)
);

CREATE TABLE watch_jobs (
  id         INTEGER PRIMARY KEY,
  channel    TEXT NOT NULL,
  thread_ts  TEXT NOT NULL,
  condition  TEXT NOT NULL,
  created_at REAL NOT NULL,
  last_run   REAL,
  done       INTEGER NOT NULL DEFAULT 0
);
"""


def _statements(script: str) -> tuple[str, ...]:
    """DDL 을 문장 단위로 나눈다.

    executescript 를 쓰지 않는 이유는 그것이 실행 직전에 열린 트랜잭션을 자동
    커밋하기 때문이다. 마이그레이션 단계 하나가 원자적이어야 해서 문장마다
    execute 로 실행한다.
    """
    return tuple(s.strip() for s in script.split(";") if s.strip())


V2_SESSION_RUNTIME_SQL = """
ALTER TABLE sessions ADD COLUMN workdir TEXT NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN model TEXT NOT NULL DEFAULT '';
"""
"""실행 환경을 대화 단위로 영속한다.

원본은 작업 디렉터리와 모델을 화자가 아니라 대화 단위로 정하고, 한 번 넓어진
값을 좁히지 않는다. 그 규칙을 지키려면 대화 단위로 저장할 컬럼이 필요하다.
빈 문자열 기본값이라 이미 있는 행은 그대로 유효하다.
"""


V3_WATCH_JOB_CONTEXT_SQL = """
ALTER TABLE watch_jobs ADD COLUMN msg_ts TEXT NOT NULL DEFAULT '';
ALTER TABLE watch_jobs ADD COLUMN checks INTEGER NOT NULL DEFAULT 0;
ALTER TABLE watch_jobs ADD COLUMN trust_level INTEGER NOT NULL DEFAULT 0;
ALTER TABLE watch_jobs ADD COLUMN extra TEXT NOT NULL DEFAULT '';
"""
"""감시 작업에 등록 시점의 맥락을 함께 저장한다.

원본 register_watch_job 은 msg_ts, checks, is_owner, org_admin 을 함께
저장했다. 그중 셋을 코어 컬럼으로 올린다.

msg_ts     감시 표식 리액션을 붙인 메시지. 없으면 완료 시 그 표식을 뗄 대상을
           지목할 수 없다. 이 컬럼이 없어 원본 동작을 재현할 수 없었다.
checks     확인 횟수. 없으면 확인 상한을 둘 수 없다.
trust_level  확인 프롬프트를 실행할 권한. 원본 is_owner 를 TrustLevel 로 일반화한
           것이라 IntEnum 값을 그대로 넣는다. 기본값 0 은 GENERAL 이다.
extra      플러그인이 쓸 JSON 문자열. 원본 org_admin 처럼 조직 전용 값은
           코어 컬럼으로 올리지 않는다.
"""


# (버전, 설명, 문장 목록). 이미 적용된 단계를 고치지 않는다. 바꿀 것은 단계를 더한다.
MIGRATIONS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (1, "초기 스키마", _statements(V1_INITIAL_SQL)),
    (2, "세션에 실행 환경 컬럼 추가", _statements(V2_SESSION_RUNTIME_SQL)),
    (3, "감시 작업에 표식 대상·확인 횟수·권한 추가", _statements(V3_WATCH_JOB_CONTEXT_SQL)),
)

SCHEMA_VERSION = MIGRATIONS[-1][0]
