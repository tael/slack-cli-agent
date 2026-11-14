"""claude 한도 문구 감지 (2026-11-06). 감지가 빗나가면 2차 전환이 일어나지 않는다."""
from __future__ import annotations

import json

import pytest

from slack_cli_agent.engine.claude import ClaudeEngine


def _payload(result: str, status=None, subtype: str = "success") -> str:
    return json.dumps({"type": "result", "subtype": subtype, "is_error": True,
                       "result": result, "api_error_status": status,
                       "session_id": "s"})


@pytest.mark.parametrize("text", [
    "You've hit your session limit · resets 11pm",
    "You've hit your weekly limit",
    "You're out of extra usage",
    "You're out of usage credits. Run /usage-credits to keep using",
    "Your org is out of usage - add funds to continue",
    "You've hit your org's monthly usage limit",
])
def test_새_한도_문구를_한도로_읽는다(text: str) -> None:
    assert ClaudeEngine._limit_from_text(_payload(text)) is not None


def test_일반_오류는_한도로_읽지_않는다() -> None:
    text = "API Error: Connection refused — a firewall or proxy may be blocking it"
    assert ClaudeEngine._limit_from_text(_payload(text)) is None


def test_종료코드_0_의_is_error_429_도_한도로_읽는다() -> None:
    engine = ClaudeEngine.__new__(ClaudeEngine)
    stdout = _payload("API Error: 429", status=429)
    resp = ClaudeEngine.parse(engine, stdout, "", 0)
    assert resp.failure_reason == "usage_limit"
    assert engine.detect_usage_limit(resp) is not None
