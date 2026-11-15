"""2026-11-15 휴가 위임 거짓 보고: audit-only check for unbacked handoff claims."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from slack_cli_agent.core.handoff_claim import find_handoff_claim, handoff_claim_unbacked
from slack_cli_agent.engine.transcript import ClaudeTranscriptReader

CLAIM = "휴가신청은 마메치에게 넘겼고 아직 상신 전임."


def test_claim_found():
    assert find_handoff_claim(CLAIM) is not None
    assert find_handoff_claim("데이전트에게 위임했음") is not None
    assert find_handoff_claim("봇이 알아서 전달했다") is not None


def test_negated_or_unrelated_not_a_claim():
    assert find_handoff_claim("마메치에게 넘기지 않았음") is None
    assert find_handoff_claim("마메치에게 넘겼다는 말은 거짓이라 않았음") is None
    assert find_handoff_claim("사람에게 넘겼다") is None
    assert find_handoff_claim("마메치 " + "가" * 30 + " 넘겼다") is None
    assert find_handoff_claim("") is None


def test_unbacked_without_post():
    assert handoff_claim_unbacked(CLAIM, ["Bash", "mcp__browser__browser_navigate"]) == CLAIM.rstrip(".")
    assert handoff_claim_unbacked(CLAIM, []) is not None


def test_backed_by_post_or_permalink():
    assert handoff_claim_unbacked(CLAIM, ["mcp__slack__slack_post_message"]) is None
    assert handoff_claim_unbacked(CLAIM + " https://x.slack.com/archives/C1/p1", []) is None


def test_unreadable_transcript_is_not_flagged():
    assert handoff_claim_unbacked(CLAIM, None) is None


def _line(kind, minute, content):
    ts = datetime(2026, 11, 15, 11, minute, tzinfo=UTC)
    return json.dumps({"type": kind, "timestamp": ts.isoformat(), "message": {"content": content}})


def _reader(tmp_path: Path, lines: list[str]) -> ClaudeTranscriptReader:
    workdir = Path("/w/dir")
    path = tmp_path / ".claude" / "projects" / "-w-dir" / "sid.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(lines))
    return ClaudeTranscriptReader(workdir, home=tmp_path)


def _use(name):
    return [{"type": "tool_use", "name": name, "input": {}}]


def test_turn_tool_names_only_after_last_real_prompt(tmp_path):
    reader = _reader(tmp_path, [
        _line("user", 0, "first question"),
        _line("assistant", 1, _use("mcp__slack__slack_post_message")),
        _line("user", 2, "second question"),
        _line("assistant", 3, _use("Bash")),
        _line("user", 4, "<task-notification>done</task-notification>"),
        _line("user", 5, "Stop hook feedback:\nrewrite"),
        _line("assistant", 6, _use("Read")),
    ])
    assert reader.turn_tool_names("sid") == ("Bash", "Read")


def test_turn_tool_names_until_ts_and_missing(tmp_path):
    reader = _reader(tmp_path, [
        _line("user", 0, "q"),
        _line("assistant", 1, _use("Bash")),
        _line("assistant", 9, _use("mcp__slack__slack_post_message")),
    ])
    cut = (datetime(2026, 11, 15, 11, 5, tzinfo=UTC) + timedelta(0)).timestamp()
    assert reader.turn_tool_names("sid", until_ts=cut) == ("Bash",)
    assert reader.turn_tool_names("nope") is None


def _pipeline_stub(tools):
    from types import SimpleNamespace

    from slack_cli_agent.core.pipeline import RequestPipeline

    rows = []
    pipe = object.__new__(RequestPipeline)
    pipe._audit = SimpleNamespace(record=lambda kind, **kw: rows.append((kind, kw)))
    pipe._engine = SimpleNamespace(name="claude")
    pipe._turn_tools = tools
    return pipe, rows


def _call(pipe, body):
    from types import SimpleNamespace

    ctx = SimpleNamespace(channel="C1", thread_ts="1.0")
    response = SimpleNamespace(session_id="sid", engine="claude")
    pipe._audit_handoff_claim(ctx, body, response, SimpleNamespace(session_id="sid"))


def test_pipeline_records_unbacked_and_survives_failure():
    pipe, rows = _pipeline_stub(lambda engine, sid: ("Bash",))
    _call(pipe, CLAIM)
    assert [kind for kind, _ in rows] == ["handoff_claim_unbacked"]

    pipe, rows = _pipeline_stub(lambda engine, sid: ("mcp__slack__slack_post_message",))
    _call(pipe, CLAIM)
    assert rows == []

    def boom(engine, sid):
        raise RuntimeError("x")

    pipe, rows = _pipeline_stub(boom)
    _call(pipe, CLAIM)  # must not raise
    assert rows == []
