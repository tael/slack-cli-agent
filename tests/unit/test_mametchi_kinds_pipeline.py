"""G7: the pipeline writes the mametchi guard kinds it used to drop."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from slack_cli_agent.guard.mentions import PlainMentionGuard
from slack_cli_agent.guard.watch import WatchPromiseGuard
from test_pipeline import FakeAwaiting, build_pipeline, make_ctx, ok_response

PROMISE = "확인해 보고 결과가 나오면 다시 알려드리겠습니다."


def _kinds(deps: dict) -> list[str]:
    return [row["kind"] for row in deps["audit"].incidents]


def test_plain_mention_fix_is_recorded(tmp_path: Path) -> None:
    pipeline, deps = build_pipeline(
        responses=[ok_response(body="@홍길동 님 확인 부탁드립니다")],
        guards=[PlainMentionGuard()], mention_table=lambda: {"홍길동": "U9"},
        tmp_path=tmp_path,
    )
    pipeline.handle(make_ctx())
    rows = [r for r in deps["audit"].incidents if r["kind"] == "plain_mention"]
    assert rows and rows[0]["names"] == ["홍길동"]


def test_untagged_promise_records_the_rewrite_request(tmp_path: Path) -> None:
    pipeline, deps = build_pipeline(
        responses=[ok_response(body=PROMISE), ok_response(body="확인했습니다.\n[[NOWATCH]]")],
        guards=[WatchPromiseGuard()], tmp_path=tmp_path,
    )
    pipeline.handle(make_ctx())
    assert "promise_without_watch" in _kinds(deps)
    assert "promise_without_watch_retry_failed" not in _kinds(deps)


def test_promise_surviving_the_rewrite_records_retry_failed(tmp_path: Path) -> None:
    pipeline, deps = build_pipeline(
        responses=[ok_response(body=PROMISE), ok_response(body=PROMISE)],
        guards=[WatchPromiseGuard()], tmp_path=tmp_path,
    )
    pipeline.handle(make_ctx())
    assert _kinds(deps).count("promise_without_watch") == 1
    rows = [r for r in deps["audit"].incidents if r["kind"] == "promise_without_watch_retry_failed"]
    assert [r["rewrite"] for r in rows] == ["still_promised"]


def test_rewrite_that_never_came_back_records_retry_failed(tmp_path: Path) -> None:
    failed = replace(ok_response(body=""), ok=False, failure_reason="timeout")
    pipeline, deps = build_pipeline(
        responses=[ok_response(body=PROMISE), failed],
        guards=[WatchPromiseGuard()], tmp_path=tmp_path,
    )
    pipeline.handle(make_ctx())
    rows = [r for r in deps["audit"].incidents if r["kind"] == "promise_without_watch_retry_failed"]
    assert [r["rewrite"] for r in rows] == ["none"]


def test_awaiting_reply_is_recorded_only_when_waiting(tmp_path: Path) -> None:
    pipeline, deps = build_pipeline(
        responses=[ok_response(body="확인했습니다.\n[[ASK]]"), ok_response(body="평범한 답입니다.")],
        tmp_path=tmp_path, awaiting=FakeAwaiting(),
    )
    pipeline.handle(make_ctx())
    pipeline.handle(make_ctx(ts="1700000001.000200"))
    rows = [r for r in deps["audit"].incidents if r["kind"] == "awaiting_reply"]
    assert [r["by"] for r in rows] == ["tag"]
