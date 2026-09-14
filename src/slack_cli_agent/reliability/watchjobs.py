"""지켜보기 큐 — "지켜보다가 끝나면 보고하겠다"는 약속을 실제로 지킨다.

`guard/watch.py` 의 `WatchPromiseGuard` 가 답변에서 `[[WATCH: ...]]` 태그를
검출해 감시 대상 설명을 뽑아내는 쪽이고, 여기는 그 설명을 실제로 지켜보는
쪽이다(원본 `register_watch_job`, `job_watch` 의 대응).

**DB 로 저장한다.** 원본은 `WATCH_JOBS_FILE`(JSON 파일)을 매 확인마다 lock
안에서 다시 읽어 병합했다. 이 계층은 그 자리를 `watch_jobs` 테이블로 대체한다
— 프로세스가 재기동해도 이 테이블에서 이어간다는 점은 원본과 같다.

등록 시점의 맥락(`msg_ts`, `trust`, `extra`)을 함께 저장한다. 확인은 등록보다
한참 뒤에 다른 프로세스에서 일어나므로, 그때 다시 구할 수 없는 값은 등록할 때
적어 둬야 한다. 컬럼 구성과 근거는 `storage/schema.py` 의 V3 단계에 있다.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..auth.principal import TrustLevel
from ..storage.database import Database
from ..storage.repository import SqliteRepository

_SELECT_COLUMNS = (
    "id, channel, thread_ts, condition, created_at, last_run, "
    "msg_ts, checks, trust_level, extra"
)


@dataclass(frozen=True)
class WatchJob:
    id: int
    channel: str
    thread_ts: str
    condition: str
    created_at: float
    last_run: float | None
    msg_ts: str = ""
    """감시 표식 리액션을 붙인 메시지. 완료 시 그 표식을 뗄 대상이다."""
    checks: int = 0
    trust: TrustLevel = TrustLevel.GENERAL
    """확인 프롬프트를 실행할 권한. 등록 시점의 요청자 권한을 그대로 잇는다."""
    extra: Mapping[str, Any] = field(default_factory=dict)
    """플러그인 몫. 조직 전용 값은 코어 필드로 올리지 않는다."""


@runtime_checkable
class WatchJobPort(Protocol):
    def enqueue(
        self,
        channel: str,
        thread_ts: str,
        condition: str,
        msg_ts: str = "",
        trust: TrustLevel = TrustLevel.GENERAL,
        extra: Mapping[str, Any] | None = None,
    ) -> int:
        """감시 큐에 등록한다. 등록한 행의 id 를 돌려준다.

        여기 넣지 않으면 그 약속은 이번 요청 처리가 끝나는 순간 아무도 다시
        보지 않는다.
        """

    def due(self, now: float, min_gap: float, max_checks: int | None = None) -> list[WatchJob]:
        """확인할 시점이 된 감시 건. 완료(done)로 표시된 것은 빠진다.

        `min_gap` 초 이내에 이미 확인한 건은 다시 나오지 않는다 — 확인
        프롬프트마다 엔진을 다시 부르는 비용이 있어 너무 잦은 확인을 막는다.
        `max_checks` 를 주면 그만큼 확인한 건도 빠진다. 그 건은 `expired` 에
        같은 상한을 줘서 회수한다 — 양쪽에서 다 빠지면 그대로 방치된다.
        """

    def mark_checked(self, job_id: int, at: float) -> None:
        """확인했지만 아직 끝나지 않았다. `last_run` 과 확인 횟수를 갱신한다."""

    def mark_done(self, job_id: int) -> None:
        """끝났다고 표시한다. 이후 `due`, `expired` 대상에서 빠진다."""

    def expired(self, now: float, max_age: float, max_checks: int | None = None) -> list[WatchJob]:
        """포기할 건. 등록한 지 `max_age` 를 넘겼거나 확인 상한을 넘긴 건이다.

        여기 걸리면 포기하고 소유자에게 알리는 것이 호출부의 몫이다 —
        직접 확인이 필요한 상태로 본다.
        """


class WatchJobQueue(SqliteRepository):
    def __init__(self, db: Database, now: Callable[[], float] = time.time) -> None:
        super().__init__(db)
        self._now = now

    def enqueue(
        self,
        channel: str,
        thread_ts: str,
        condition: str,
        msg_ts: str = "",
        trust: TrustLevel = TrustLevel.GENERAL,
        extra: Mapping[str, Any] | None = None,
    ) -> int:
        cursor = self._execute(
            "INSERT INTO watch_jobs (channel, thread_ts, condition, created_at, "
            "last_run, done, msg_ts, checks, trust_level, extra) "
            "VALUES (?, ?, ?, ?, NULL, 0, ?, 0, ?, ?)",
            (
                channel, thread_ts, condition, self._now(),
                msg_ts, int(trust), _dump_extra(extra),
            ),
        )
        return int(cursor.lastrowid)

    def due(self, now: float, min_gap: float, max_checks: int | None = None) -> list[WatchJob]:
        sql = (
            f"SELECT {_SELECT_COLUMNS} FROM watch_jobs WHERE done = 0 "
            "AND (? - COALESCE(last_run, created_at)) >= ?"
        )
        params: tuple[Any, ...] = (now, min_gap)
        if max_checks is not None:
            sql += " AND checks < ?"
            params += (max_checks,)
        return [_to_job(row) for row in self._fetch_all(sql, params)]

    def open_count(self) -> int:
        """아직 끝나지 않은 감시 작업 수. 상태 기록이 쓴다."""
        row = self._fetch_one("SELECT COUNT(*) AS n FROM watch_jobs WHERE done = 0")
        return int(row["n"]) if row else 0

    def mark_checked(self, job_id: int, at: float) -> None:
        self._execute(
            "UPDATE watch_jobs SET last_run = ?, checks = checks + 1 WHERE id = ?",
            (at, job_id),
        )

    def mark_done(self, job_id: int) -> None:
        self._execute("UPDATE watch_jobs SET done = 1 WHERE id = ?", (job_id,))

    def expired(self, now: float, max_age: float, max_checks: int | None = None) -> list[WatchJob]:
        sql = f"SELECT {_SELECT_COLUMNS} FROM watch_jobs WHERE done = 0 AND ((? - created_at) >= ?"
        params: tuple[Any, ...] = (now, max_age)
        if max_checks is not None:
            sql += " OR checks >= ?"
            params += (max_checks,)
        sql += ")"
        return [_to_job(row) for row in self._fetch_all(sql, params)]


def _dump_extra(extra: Mapping[str, Any] | None) -> str:
    """빈 값은 빈 문자열로 적는다. '{}' 로 적으면 안 쓴 것과 구분이 안 된다."""
    if not extra:
        return ""
    return json.dumps(dict(extra), ensure_ascii=False)


def _load_extra(raw: str | None) -> dict[str, Any]:
    """깨진 값이면 빈 dict 로 본다.

    사람이 손댔거나 옛 판이 쓴 값이 JSON 이 아닐 수 있다. 그 한 건 때문에
    감시 조회 전체가 예외로 끝나면 나머지 건도 확인되지 않는다.
    """
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _to_job(row: Any) -> WatchJob:
    return WatchJob(
        id=int(row["id"]),
        channel=row["channel"],
        thread_ts=row["thread_ts"],
        condition=row["condition"],
        created_at=float(row["created_at"]),
        last_run=float(row["last_run"]) if row["last_run"] is not None else None,
        msg_ts=row["msg_ts"] or "",
        checks=int(row["checks"]),
        trust=TrustLevel(int(row["trust_level"])),
        extra=_load_extra(row["extra"]),
    )
