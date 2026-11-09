"""수신자 표식 재시도 턴에서는 Stop 훅이 판정하지 않는다 (2026-11-09 private-taeil 20:31)."""

from __future__ import annotations

from slack_cli_agent.core.markers import missing_recipient_prompt
from slack_cli_agent.guard.finish_hook import _is_recipient_retry_turn


def _user(text: str, meta: bool = False) -> dict:
    row = {"type": "user", "message": {"role": "user", "content": text}}
    if meta:
        row["isMeta"] = True
    return row


def _assistant(text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def test_재시도_지시_뒤_훅_피드백과_재촉이_끼어도_재시도_턴으로_본다() -> None:
    rows = [
        _user("원래 질문"), _assistant("답"),
        _user(missing_recipient_prompt()), _assistant("답\n[[RECIPIENT:ASKER]]"),
        _user("Stop hook feedback:\n다음 문장은 근거가 없다", meta=True),
        _user("[Your previous response had no visible output.]", meta=True),
    ]
    assert _is_recipient_retry_turn(rows)


def test_보통_턴은_재시도_턴이_아니다() -> None:
    rows = [_user(missing_recipient_prompt()), _assistant("답"), _user("재발방지."), _assistant("고쳤습니다")]
    assert not _is_recipient_retry_turn(rows)
