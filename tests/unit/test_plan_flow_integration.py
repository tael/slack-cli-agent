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


def test_작업자_호출은_plan_gate_가_판정하지_않는다(tmp_path: Path, monkeypatch: Any) -> None:
    """작업자(Agent) 안의 도구 호출도 PreToolUse 훅을 거치고, 입력에는 메인 세션
    transcript_path 와 agent_id 가 온다 (claude 2.1.321, 2026-11-10 확인). 메인이
    계획 없이 2번째 응답에서 Agent 를 부르면 작업자 호출이 3번째로 세여 거부되고,
    거부 기록이 메인 전사에 남지 않아 풀리지 않는다. 작업자 호출은 통과시킨다."""
    import json

    from slack_cli_agent.guard.pretool import plan_gate as pg

    lines = [
        {"type": "user", "message": {"content": "조사해 줘"}},
        {"type": "assistant", "message": {"id": "m1", "content": [
            {"type": "tool_use", "id": "u1", "name": "Read", "input": {}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "u1", "content": "x"}]}},
        {"type": "assistant", "message": {"id": "m2", "content": [
            {"type": "tool_use", "id": "u2", "name": "Agent", "input": {}}]}},
    ]
    transcript = tmp_path / "main.jsonl"
    transcript.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in lines) + "\n")
    monkeypatch.setenv(PLAN_GATE_ENV, "3")
    worker_call = {"transcript_path": str(transcript), "tool_use_id": "w1", "tool_name": "Read"}
    assert pg.decide(worker_call)[0] == "deny"
    assert pg.decide({**worker_call, "agent_id": "a1", "agent_type": "lookup"})[0] == "subagent"
