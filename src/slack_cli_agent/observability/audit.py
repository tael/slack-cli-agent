"""감사 기록. DB(`audit` 테이블)와 `audit.jsonl` 파일에 함께 남긴다.

외부 도구와 대시보드가 jsonl 형식을 읽으므로, 저장할 때 원본 필드 이름을
그대로 쓴다. DB 는 조회·집계용이고 jsonl 은 외부 도구와의 접점이다 — 어느
한쪽만 쓰면 그쪽 소비자가 못 읽는다.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..storage.database import Database
from ..storage.repository import SqliteRepository

# 요청 처리 한 건의 감사 기록 종류. 원본은 이 항목에 kind 를 남기지 않았으나
# audit 테이블의 kind 컬럼이 NOT NULL 이라 이름을 붙인다.
REQUEST_KIND = "request"


class AuditLog(SqliteRepository):
    def __init__(
        self,
        db: Database,
        jsonl_path: Path,
        now: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(db)
        self._jsonl_path = jsonl_path
        self._now = now

    def record(
        self,
        kind: str,
        *,
        channel: str = "",
        thread_ts: str = "",
        **fields: Any,
    ) -> None:
        """감사 기록 한 건을 DB 와 jsonl 에 함께 남긴다.

        `fields` 는 종류마다 다른 부가 정보다. DB 의 `payload` 컬럼과 jsonl 줄에
        같은 내용이 담긴다.
        """
        at = self._now()
        self._execute(
            "INSERT INTO audit (at, kind, channel, thread_ts, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (at, kind, channel, thread_ts, json.dumps(fields, ensure_ascii=False)),
        )
        line = {"kind": kind, "channel": channel, "thread_ts": thread_ts, "at": at}
        line.update(fields)
        self._append_jsonl(line)

    def record_request(
        self,
        *,
        channel: str,
        thread_ts: str,
        message_ts: str,
        session_id: str,
        resumed: bool,
        model: str,
        effort: str,
        elapsed: float,
        ok: bool,
        first_reaction_sec: float | None = None,
        queue_wait_sec: float | None = None,
        usage: Mapping[str, Any] | None = None,
        **extra: Any,
    ) -> None:
        """요청 처리 한 건의 감사 기록.

        원본이 남기던 항목 — 첫 반응까지 걸린 시간, 대기 시간, 세션, 모델,
        effort, 소요, 토큰(usage), 성공 여부 — 을 그대로 담는다.
        """
        self.record(
            REQUEST_KIND,
            channel=channel,
            thread_ts=thread_ts,
            message_ts=message_ts,
            session_id=session_id,
            resumed=resumed,
            model=model,
            effort=effort,
            elapsed=elapsed,
            ok=ok,
            first_reaction_sec=first_reaction_sec,
            queue_wait_sec=queue_wait_sec,
            usage=dict(usage) if usage is not None else None,
            **extra,
        )

    def _append_jsonl(self, line: Mapping[str, Any]) -> None:
        self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
