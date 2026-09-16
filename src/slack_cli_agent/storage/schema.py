"""Schema definition and migration steps.

Each version adds one step; an existing DB applies steps after its current
`user_version` in order. Fresh DBs and upgraded DBs go through the same path
and converge on the same schema.
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
    """Split DDL into statements.

    Not using executescript because it auto-commits any transaction open
    before it runs, and each migration step needs to be atomic.
    """
    return tuple(s.strip() for s in script.split(";") if s.strip())


V2_SESSION_RUNTIME_SQL = """
ALTER TABLE sessions ADD COLUMN workdir TEXT NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN model TEXT NOT NULL DEFAULT '';
"""
"""Persists the runtime environment (workdir, model) per conversation rather
than per speaker; existing rows stay valid via the empty-string default.
"""


V3_WATCH_JOB_CONTEXT_SQL = """
ALTER TABLE watch_jobs ADD COLUMN msg_ts TEXT NOT NULL DEFAULT '';
ALTER TABLE watch_jobs ADD COLUMN checks INTEGER NOT NULL DEFAULT 0;
ALTER TABLE watch_jobs ADD COLUMN trust_level INTEGER NOT NULL DEFAULT 0;
ALTER TABLE watch_jobs ADD COLUMN extra TEXT NOT NULL DEFAULT '';
"""
"""Stores the context a watch job was registered with.

msg_ts       the message carrying the watch-mark reaction, needed to remove
             that mark once the watch completes.
checks       number of checks so far, needed to enforce a check cap.
trust_level  permission level for running the check prompt (an IntEnum value;
             0 is GENERAL).
extra        JSON string for plugin-specific data that shouldn't be a core column.
"""


V4_CONNECTION_EPOCHS_SQL = """
CREATE TABLE connection_epochs (
  generation     INTEGER PRIMARY KEY AUTOINCREMENT,
  kind           TEXT    NOT NULL,
  connected_at   REAL    NOT NULL,
  gap_started_at REAL    NOT NULL,
  last_seen_at   REAL    NOT NULL,
  state          TEXT    NOT NULL DEFAULT 'PENDING',
  lease_owner    TEXT    NOT NULL DEFAULT '',
  lease_until    REAL,
  attempts       INTEGER NOT NULL DEFAULT 0,
  completed_at   REAL
);
CREATE INDEX idx_connection_epochs_state ON connection_epochs(state, generation);
"""
"""Ledger of socket connections, written by ingress and read by workers.

Ingress owns the socket, the worker runs catch-up, and nothing carried that
fact across — an ingress-only restart went unswept (measured 2026-09-16).

generation      monotonic, so reconnects don't collapse while a worker is busy
gap_started_at  last time the socket was known up before this epoch
last_seen_at    liveness mark for this epoch; becomes the next gap_started_at
lease_owner     several workers can run, so one claims the span at a time
"""

# (version, label, statements). Never edit an applied step; add a new one instead.
V5_WATCH_JOB_RUN_SQL = """
ALTER TABLE watch_jobs ADD COLUMN workdir TEXT NOT NULL DEFAULT '';
ALTER TABLE watch_jobs ADD COLUMN run_id TEXT NOT NULL DEFAULT '';
"""
"""Pins where a watch job's background work actually runs.

workdir  absolute working directory at registration time. The check runs
         later in another process; recomputing it from the current channel
         config would look in a different place if that config changed.
run_id   identifier for this job's result file. The column lands here; the
         code that issues it comes with sca-17p, so rows written until then
         leave it empty.
"""


MIGRATIONS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (1, "초기 스키마", _statements(V1_INITIAL_SQL)),
    (2, "세션에 실행 환경 컬럼 추가", _statements(V2_SESSION_RUNTIME_SQL)),
    (3, "감시 작업에 표식 대상·확인 횟수·권한 추가", _statements(V3_WATCH_JOB_CONTEXT_SQL)),
    (4, "소켓 연결 세대 원장 추가", _statements(V4_CONNECTION_EPOCHS_SQL)),
    (5, "감시 작업에 실행 자리와 결과 파일 이름 추가", _statements(V5_WATCH_JOB_RUN_SQL)),
)

SCHEMA_VERSION = MIGRATIONS[-1][0]
