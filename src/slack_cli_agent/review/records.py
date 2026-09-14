"""지목한 답변을 만든 감사 기록 한 건을 찾는다.

원본 bot.py 의 clean_excerpt(), find_answer_record() 를
그대로 옮겼다. 원본은 audit.jsonl 을 뒤에서 4000줄 읽어 대상을 추렸으나,
여기서는 같은 정보를 이미 담고 있는 `audit` DB 테이블(kind='request')을
조회한다. jsonl 재파싱과 달리 SQL 조회 하나로 끝난다.

본문 대조를 먼저 한다. 같은 스레드에서 여러 번 답했을 때 어느 답인지
가르는 것은 본문뿐이다. 리치 채널은 슬랙이 준 text 가 알림 미리보기라
저장된 원문과 다를 수 있어, 못 찾으면 같은 스레드의 마지막 성공 기록으로
물러선다.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

from slack_cli_agent.observability.audit import REQUEST_KIND
from slack_cli_agent.storage.database import Database
from slack_cli_agent.storage.repository import SqliteRepository

# 최근 몇 건까지 볼지. 원본이 audit.jsonl 뒤에서 4000줄만 읽던 것과 같은 상한이다.
_LOOKBACK_LIMIT = 4000

_LINK_LABEL = re.compile(r"<([^<>|]+)\|([^<>]+)>")
_BRACKET = re.compile(r"<([^<>]+)>")


def clean_excerpt(text: str | None) -> str | None:
    """슬랙 원문 일부를 새 메시지에 그대로 옮겨 적을 때 읽을 수 있게 다듬는다.

    슬랙 이벤트 API는 원문을 &lt; &gt; &amp; 로 이스케이프해서 준다. 그대로
    새 메시지에 박으면 그 글자가 그대로 보인다. 게다가 이미 렌더된 슬랙 링크
    <url|라벨> 을 복사해 붙이면 슬랙이 또 한 번 꺾쇠로 감싸 겹으로 남는다.
    엔티티를 풀고, 링크 표기는 라벨만 남기고, 남은 꺾쇠는 더 이상 안 바뀔
    때까지 벗겨서 최소한 사람이 읽을 수 있는 글로 만든다.
    """
    if not text:
        return text
    out = html.unescape(text)
    out = _LINK_LABEL.sub(r"\2", out)
    prev = None
    while prev != out:
        prev = out
        out = _BRACKET.sub(r"\1", out)
    return out


class AnswerRecordFinder(SqliteRepository):
    """`audit` 테이블에서 지목한 답변의 실행 기록을 찾는다."""

    def __init__(self, database: Database) -> None:
        super().__init__(database)

    def find(self, channel: str, thread_ts: str, text: str) -> dict[str, Any] | None:
        rows = self._fetch_all(
            "SELECT payload, thread_ts FROM audit "
            "WHERE kind = ? AND channel = ? ORDER BY id DESC LIMIT ?",
            (REQUEST_KIND, channel, _LOOKBACK_LIMIT),
        )
        recs: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or not payload.get("answer"):
                continue
            payload = dict(payload)
            payload["thread_ts"] = row["thread_ts"]
            recs.append(payload)

        # rows 는 이미 최신(id DESC) 순이므로, 원본의 `reversed(recs)`
        # (오래된 순으로 쌓은 목록을 최신 순으로 훑는 것)와 그대로 같다.
        probe = (clean_excerpt(text or "") or "").strip()[:60]
        if probe:
            for r in recs:
                if probe in r["answer"]:
                    return r
        for r in recs:
            if r.get("thread_ts") == thread_ts and r.get("ok"):
                return r
        return None
