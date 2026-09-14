"""부검·디버그 추적·서식 점검의 중복 방지 원장.

원본 bot.py 는 이 세 점검을 각각 별도의 jsonl 파일로
관리했다(load_postmortems/save_postmortem/drop_postmortem 계열이 셋 반복).
새 코드베이스에는 이미 이 목적을 위한 `reviews` 테이블
(kind, channel, target_ts, at, result — PK 는 앞의 세 열)이 스키마 V1 에
있어, ReviewLedger 하나로 세 벌을 합친다. `kind` 값으로 점검 종류를 가른다
("postmortem", "debug_trace", "format_review").

`result` 열에는 상태를 JSON 문자열로 담는다. 사람이 직접 손댔거나 예전 판이
남긴 값이 JSON 이 아닐 수 있으므로, 그런 값은 예외를 내지 않고 빈 상태로
본다.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass

from slack_cli_agent.storage.database import Database
from slack_cli_agent.storage.repository import SqliteRepository


@dataclass(frozen=True)
class ReviewRecord:
    """`reviews.result` 열의 JSON 을 풀어낸 값.

    status 는 "진행" 또는 "완료" 다. 빈 문자열은 JSON 파싱에 실패했거나
    아직 채워지지 않은 값을 뜻한다.
    """

    status: str = ""
    by: str = ""
    link: str = ""
    report: str = ""


class ReviewLedger(SqliteRepository):
    """`reviews` 테이블을 감싸 점검 종류별 중복 방지·상태 조회를 제공한다."""

    def __init__(self, database: Database, now: Callable[[], float] | None = None) -> None:
        super().__init__(database)
        self._now = now or time.time

    def is_reviewed(self, kind: str, channel: str, target_ts: str) -> bool:
        """이 대상에 대해 이미 시작했거나 끝난 점검이 있는가."""
        return self.find(kind, channel, target_ts) is not None

    def begin(self, kind: str, channel: str, target_ts: str, *, by: str) -> None:
        """점검을 시작한 것으로 기록한다. 중간에 실패해도 재시도(drop) 전까지는
        중복 방지 대상으로 남는다."""
        self._store(kind, channel, target_ts, ReviewRecord(status="진행", by=by))

    def complete(
        self,
        kind: str,
        channel: str,
        target_ts: str,
        *,
        by: str,
        link: str = "",
        report: str = "",
    ) -> None:
        """점검을 완료한 것으로 기록한다."""
        self._store(
            kind,
            channel,
            target_ts,
            ReviewRecord(status="완료", by=by, link=link, report=report),
        )

    def drop(self, kind: str, channel: str, target_ts: str) -> None:
        """기록을 지운다. 실패한 시도를 되돌려 재시도할 수 있게 한다.

        기록이 없어도 예외를 내지 않는다.
        """
        self._execute(
            "DELETE FROM reviews WHERE kind = ? AND channel = ? AND target_ts = ?",
            (kind, channel, target_ts),
        )

    def find(self, kind: str, channel: str, target_ts: str) -> ReviewRecord | None:
        """이 대상의 현재 상태를 읽는다. 기록이 없으면 None."""
        row = self._fetch_one(
            "SELECT result FROM reviews WHERE kind = ? AND channel = ? AND target_ts = ?",
            (kind, channel, target_ts),
        )
        if row is None:
            return None
        return self._parse(row["result"])

    def _store(self, kind: str, channel: str, target_ts: str, record: ReviewRecord) -> None:
        payload = json.dumps(
            {
                "status": record.status,
                "by": record.by,
                "link": record.link,
                "report": record.report,
            },
            ensure_ascii=False,
        )
        self._execute(
            "INSERT OR REPLACE INTO reviews (kind, channel, target_ts, at, result) "
            "VALUES (?, ?, ?, ?, ?)",
            (kind, channel, target_ts, self._now(), payload),
        )

    @staticmethod
    def _parse(raw: str) -> ReviewRecord:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return ReviewRecord(status="")
        if not isinstance(data, dict):
            return ReviewRecord(status="")
        return ReviewRecord(
            status=str(data.get("status", "")),
            by=str(data.get("by", "")),
            link=str(data.get("link", "")),
            report=str(data.get("report", "")),
        )
