"""claude -p --output-format json 이 이벤트 배열을 내는 경우.

2026-09-15 실측 — CLI 가 단일 객체가 아니라 [system, assistant, result]
형태의 배열을 낸다. 파서가 dict 만 받아 bad_json 으로 떨어졌고, 봇이
답을 못 올렸다. 관측 지점은 실제 CLI 출력이다.
"""

from __future__ import annotations

import json

from slack_cli_agent.config.profile import Profile
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.engine.claude import ClaudeEngine


def engine() -> ClaudeEngine:
    profile = Profile.from_dict({
        "name": "t",
        "primary_engine": {"type": "claude", "binary": "claude", "model": "claude-opus-5"},
        "state_dir": "/tmp/engine-parse-test",
    })
    return ClaudeEngine(profile, RuntimeSettings())


def result_event(**overrides: object) -> dict[str, object]:
    event: dict[str, object] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "2입니다.",
        "session_id": "sess-abc",
        "num_turns": 1,
        "usage": {"input_tokens": 2, "output_tokens": 7},
    }
    event.update(overrides)
    return event


def test_이벤트_배열에서_result_를_읽는다() -> None:
    stdout = json.dumps([
        {"type": "system", "subtype": "init", "session_id": "sess-abc", "model": "claude-opus-5"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "2입니다."}]}},
        result_event(),
    ], ensure_ascii=False)

    response = engine().parse(stdout, "", 0)

    assert response.ok
    assert response.body == "2입니다."
    assert response.session_id == "sess-abc"
    assert response.turns == 1


def test_이벤트_배열의_실패도_읽는다() -> None:
    stdout = json.dumps([
        {"type": "system", "subtype": "init"},
        result_event(is_error=True, subtype="error_max_turns", result=""),
    ], ensure_ascii=False)

    response = engine().parse(stdout, "", 0)

    assert not response.ok
    assert response.failure_reason == "is_error"


def test_단일_객체도_그대로_읽는다() -> None:
    response = engine().parse(json.dumps(result_event(), ensure_ascii=False), "", 0)

    assert response.ok
    assert response.body == "2입니다."


def test_result_이벤트가_없으면_bad_json() -> None:
    stdout = json.dumps([{"type": "system", "subtype": "init"}], ensure_ascii=False)

    response = engine().parse(stdout, "", 0)

    assert not response.ok
    assert response.failure_reason == "bad_json"
