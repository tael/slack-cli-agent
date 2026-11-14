"""Codex 워크스페이스 크레딧 소진을 사용량 한도로 판정한다.

크레딧이 바닥나면 이 요청도 다음 요청도 똑같이 실패한다. 사용량 한도와 같은
계정 단위 벽이라 FallbackEngine 이 대체 엔진으로 넘겨야 한다. 예전에는
CodexEngine.detect_usage_limit 이 늘 None 을 돌려 일반 실패로 끝났다.
문구는 마메치 bot.py 의 CODEX_CREDIT_HINTS 와 같다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from test_engine import (
    SETTINGS,
    FakeCompleted,
    RecordingEngine,
    codex_profile,
    named,
    request,
    통과정책,
)

from slack_cli_agent.engine.base import EngineResponse
from slack_cli_agent.engine.codex import CodexEngine
from slack_cli_agent.engine.runner import EngineRunner, FallbackEngine
from slack_cli_agent.engine.switcher import EngineSwitcher

#: codex exec --json 이 크레딧 소진으로 끝날 때의 JSONL 형태.
CREDIT_STDOUT = "\n".join([
    json.dumps({"type": "thread.started", "thread_id": "th-credit"}),
    json.dumps({"type": "error", "message": "Your workspace is out of credits."}),
    json.dumps({"type": "turn.failed",
                "error": {"message": "Your workspace is out of credits."}}),
])


def _engine(tmp_path: Path) -> CodexEngine:
    return CodexEngine(codex_profile(tmp_path), SETTINGS)

#: 도구 출력에 크레딧 문구가 있고 다른 이유(스트림 끊김)로 실패한 턴.
#: 봇이 이 리포나 마메치 bot.py 의 CODEX_CREDIT_HINTS 줄을 읽은 경우다.
TOOL_OUTPUT_STDOUT = "\n".join([
    json.dumps({"type": "thread.started", "thread_id": "th-tool"}),
    json.dumps({"type": "item.completed", "item": {
        "type": "command_execution", "command": "grep -n CODEX_CREDIT_HINTS bot.py",
        "aggregated_output":
            '2500:CODEX_CREDIT_HINTS = ("workspace is out of credits", "out of credits")\n',
        "exit_code": 0, "status": "completed"}}),
    json.dumps({"type": "item.completed", "item": {
        "type": "mcp_tool_call", "server": "slack", "tool": "conversations_add_message",
        "status": "completed"}}),
    json.dumps({"type": "item.completed", "item": {
        "type": "agent_message", "text": "out of credits 문구를 찾았습니다."}}),
    json.dumps({"type": "error", "message": "stream disconnected before completion"}),
    json.dumps({"type": "turn.failed",
                "error": {"message": "stream disconnected before completion"}}),
])


class Test크레딧_소진_판정:
    def test_크레딧_소진은_사용량_한도다(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        response = engine.parse(CREDIT_STDOUT, "", 1)
        limit = engine.detect_usage_limit(response)
        assert limit is not None
        assert "out of credits" in limit.detail.lower()

    def test_stderr_에_나와도_판정한다(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        response = engine.parse("", "ERROR: workspace is out of credits", 1)
        assert engine.detect_usage_limit(response) is not None

    def test_일반_실패는_한도가_아니다(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        response = engine.parse("", "그냥 실패", 1)
        assert engine.detect_usage_limit(response) is None

    def test_MCP_401_은_한도가_아니다(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        stderr = "ERROR rmcp::transport: MCP server notion: 401 Unauthorized"
        response = engine.parse("", stderr, 1)
        assert engine.detect_usage_limit(response) is None

    def test_성공한_답에_문구가_있어도_한도가_아니다(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        stdout = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "th-ok"}),
            json.dumps({"type": "item.completed", "item": {
                "type": "agent_message", "text": "out of credits 는 크레딧 소진 문구입니다."}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}),
        ])
        response = engine.parse(stdout, "", 0)
        assert response.ok
        assert engine.detect_usage_limit(response) is None

    def test_실패_본문만으로는_판정하지_않는다(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        response = EngineResponse(
            ok=False, body="out of credits 가 무슨 뜻인지 물어보셨습니다.", session_id=None,
            model_actual=None, elapsed=0.0, turns=None, usage=None,
            raw={"stderr": "timeout after 600s"}, failure_reason="timeout",
        )
        assert engine.detect_usage_limit(response) is None


    def test_도구_출력과_모델_메시지의_문구는_판정하지_않는다(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        response = engine.parse(TOOL_OUTPUT_STDOUT, "", 1)
        assert not response.ok
        assert engine.detect_usage_limit(response) is None

    def test_turn_failed_오류_메시지만_있어도_판정한다(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        stdout = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "th-credit"}),
            json.dumps({"type": "turn.failed",
                        "error": {"message": "Your workspace is out of credits."}}),
        ])
        limit = engine.detect_usage_limit(engine.parse(stdout, "", 1))
        assert limit is not None
        assert limit.detail == "Your workspace is out of credits."


class Test크레딧_소진이면_대체_엔진으로_전환한다:
    def test_FallbackEngine_이_2차로_답한다(self, tmp_path: Path) -> None:
        profile = codex_profile(tmp_path)
        primary = _engine(tmp_path)
        secondary = named(RecordingEngine, "claude")(profile, SETTINGS)
        switcher = EngineSwitcher(tmp_path / "engine_state.json")

        def fake(cmd: list[str], cwd: Any, timeout: Any, **kwargs: Any) -> FakeCompleted:
            if cmd[0] == "fake-bin":
                return FakeCompleted(stdout="2차 답", returncode=0)
            return FakeCompleted(stdout=CREDIT_STDOUT, returncode=1)

        runner = EngineRunner(SETTINGS, subprocess_runner=fake, environment_policy=통과정책())
        engine = FallbackEngine(primary, secondary, switcher, runner)

        response = engine.run(request(model=None))

        assert response.ok
        assert response.body == "2차 답"
        assert switcher.load().get("approval") == "approved"

    def test_도구_출력에_문구가_있는_실패는_전환하지_않는다(self, tmp_path: Path) -> None:
        profile = codex_profile(tmp_path)
        primary = _engine(tmp_path)
        secondary = named(RecordingEngine, "claude")(profile, SETTINGS)
        switcher = EngineSwitcher(tmp_path / "engine_state.json")
        calls: list[str] = []

        def fake(cmd: list[str], cwd: Any, timeout: Any, **kwargs: Any) -> FakeCompleted:
            calls.append(cmd[0])
            if cmd[0] == "fake-bin":
                return FakeCompleted(stdout="2차 답", returncode=0)
            return FakeCompleted(stdout=TOOL_OUTPUT_STDOUT, returncode=1)

        runner = EngineRunner(SETTINGS, subprocess_runner=fake, environment_policy=통과정책())
        engine = FallbackEngine(primary, secondary, switcher, runner)

        response = engine.run(request(model=None))

        assert not response.ok
        assert "fake-bin" not in calls
        assert switcher.load().get("approval") != "approved"
