"""#188-#191 통합 교차 시험 (W5).

파이프라인이 만든 실제 EngineRequest 를 agents 가 있는 프로필의 ClaudeEngine 에
넣어, 소유자 턴과 base_tools 턴이 각각 무엇을 받는지 한 번에 본다.
- 소유자 턴 : --agents (#190), PLAN_GATE 가 엔진 프로세스 환경까지 간다 (#191),
  payload 감사 행 tools_builtin 에 Agent 가 남는다 (#189)
- base_tools 턴 : --agents 도 PLAN_GATE 도 없다
- FLOW_NOTE 가 이름으로 부르는 작업자 lookup, analyst 가 견본 프로필 정의에 있다
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from slack_cli_agent.auth.tools import ToolPolicy
from slack_cli_agent.config.profile import Profile
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.engine.base import EngineRequest
from slack_cli_agent.engine.claude import ClaudeEngine
from slack_cli_agent.engine.environment import ClaudeEnvironmentPolicy
from slack_cli_agent.guard.pretool.plan_gate import PLAN_GATE_ENV
from slack_cli_agent.prompt.library import PromptLibrary

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "profiles" / "example.example.json"

OWNER_TOOLS = ("Bash", "Agent", "mcp__snowflake__run_snowflake_query")


def _requests(tmp_path: Path) -> tuple[EngineRequest, EngineRequest]:
    from test_pipeline import build_pipeline, make_ctx, ok_response

    policy = ToolPolicy(base_tools=("Read", "Grep", "Glob"), owner_tools=OWNER_TOOLS)
    pipeline, deps = build_pipeline(
        responses=[ok_response(), ok_response()], tmp_path=tmp_path, tool_policy=policy,
    )
    pipeline._plan_gate_responses = RuntimeSettings().plan_gate_responses
    pipeline.handle(make_ctx(channel="C1", user="UOWNER", ts="1700000001.000100",
                             thread_ts="1700000001.000100"))
    pipeline.handle(make_ctx(channel="C1", user="U2", ts="1700000002.000100",
                             thread_ts="1700000002.000100"))
    owner, base = deps["runner"].calls
    return owner, base


def _engine() -> ClaudeEngine:
    profile = Profile._read(EXAMPLE)
    assert set(profile.agents) >= {"lookup", "analyst"}
    return ClaudeEngine(profile, RuntimeSettings())


def _child_env(request: EngineRequest) -> dict[str, Any]:
    policy = ClaudeEnvironmentPolicy(profile_name="example", home_dir=None)
    return policy.build({"PATH": "/usr/bin", **request.engine_env})


def test_소유자_턴은_agents_와_PLAN_GATE_를_함께_받는다(tmp_path: Path) -> None:
    owner, _ = _requests(tmp_path)
    assert "Agent" in owner.tools.names
    cmd = _engine().build_command(owner)
    assert "--agents" in cmd
    assert _child_env(owner).get(PLAN_GATE_ENV) == "3"


def test_base_tools_턴은_둘_다_받지_않는다(tmp_path: Path) -> None:
    _, base = _requests(tmp_path)
    assert "Agent" not in base.tools.names
    cmd = _engine().build_command(base)
    assert "--agents" not in cmd
    assert PLAN_GATE_ENV not in base.engine_env
    assert PLAN_GATE_ENV not in _child_env(base)


def test_감사_행은_Agent_가_열린_턴에만_Agent_를_남긴다(tmp_path: Path) -> None:
    owner, base = _requests(tmp_path)
    engine = _engine()
    assert "Agent" in engine.footprint_for(owner).as_audit_dict()["tools_builtin"]
    assert "Agent" not in engine.footprint_for(base).as_audit_dict()["tools_builtin"]


def test_FLOW_NOTE_가_부르는_작업자가_견본_정의에_있다() -> None:
    note = PromptLibrary(Path("/does/not/exist")).text("FLOW_NOTE")
    named = set(re.findall(r'subagent_type="(\w+)"', note))
    assert named == {"lookup", "analyst"}
    assert named <= set(Profile._read(EXAMPLE).agents)
    for name in named:
        assert (REPO / "profiles" / "agents" / f"{name}.md").is_file()


def test_전사에_남은_훅_거부_문구를_거부로_센다() -> None:
    """claude 2.1.321 은 PreToolUse 거부를 "PreToolUse:<도구> hook error: <사유>" 로
    tool_result 에 남긴다 (2026-11-10 W5 측정 전사). 접두를 떼지 않으면 거부가 0회로
    세여 serial_read_guard 의 재호출 허용이 동작하지 않았다."""
    from slack_cli_agent.guard.pretool import serial_read_guard as srg

    def turn(reason: str, n: int) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = [{"type": "user", "message": {"content": "조사해 줘"}}]
        for i in range(n):
            records.append({"type": "assistant", "message": {"id": f"m{i}", "content": [
                {"type": "tool_use", "id": f"t{i}", "name": "Read", "input": {"file_path": "/x"}}]}})
            records.append({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": f"t{i}", "is_error": True,
                 "content": f"PreToolUse:Read hook error: {reason}"}]}})
        return records

    serial = srg.responses_since_prompt(turn(srg.MARKER + " x", 2))
    assert all(u.denied for r in serial for u in r.uses)
