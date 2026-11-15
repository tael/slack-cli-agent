"""위임 안내는 이 턴 도구 목록에 Agent(또는 Task)가 있을 때만 보인다.

신지 프로필은 소유자 턴 도구에 Agent 도 agents 정의도 없는데, 기본 처리 흐름 지침과
훅 문구가 Agent(subagent_type="lookup"|"analyst") 위임을 안내했다. 실행할 수 없는
도구를 안내하는 것이므로, 파이프라인이 도구 목록을 보고 AGENT_DELEGATION 을 훅
환경에 싣고 FlowSection 이 같은 판단(CompositionContext.delegation)을 받는다.
환경변수가 없으면 위임 문구 없음이 기본값이다.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from slack_cli_agent.auth.principal import Principal, TrustLevel, TurnTier
from slack_cli_agent.auth.tools import ToolPolicy
from slack_cli_agent.engine.environment import ClaudeEnvironmentPolicy
from slack_cli_agent.engine.tool_selection import ToolSelection
from slack_cli_agent.guard import plan_start as ps
from slack_cli_agent.guard.posttool import parallel_nudge as pn
from slack_cli_agent.guard.pretool import plan_gate as pg
from slack_cli_agent.guard.pretool import serial_read_guard as srg
from slack_cli_agent.prompt.library import PromptLibrary
from slack_cli_agent.prompt.sections import CompositionContext, FlowSection

DELEGATION_ENV = "AGENT_DELEGATION"
_PRINCIPAL = Principal(user_id="U1", channel="C1", trust=TrustLevel.GENERAL, is_direct_message=False)
_PLAN = {
    "type": "branch",
    "branches": [
        {"goal": "상점 목록", "source": "stores.csv", "depends_on": [], "delegate": "lookup",
         "returns": "목록"},
        {"goal": "주문 수", "source": "ORDERS", "depends_on": [], "delegate": "none",
         "returns": "수치"},
    ],
}


def _ctx(**overrides: object) -> CompositionContext:
    base: dict[str, object] = {
        "principal": _PRINCIPAL,
        "library": PromptLibrary(Path("/does/not/exist")),
        "turn_tier": TurnTier.OWNER.value,
    }
    base.update(overrides)
    return CompositionContext(**base)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch: Any) -> None:
    monkeypatch.delenv(DELEGATION_ENV, raising=False)


# ---- ToolSelection.can_delegate ---------------------------------------------


class TestCanDelegate:
    @pytest.mark.parametrize("names", [("Read", "Agent"), ("Task",), ("Bash", "Agent(lookup)")])
    def test_Agent_나_Task_가_허용_목록에_있으면_참(self, names: tuple[str, ...]) -> None:
        assert ToolSelection.allow(names).can_delegate is True

    @pytest.mark.parametrize("selection", [
        ToolSelection.allow(("Read", "Bash")),
        ToolSelection.allow(("mcp__x__Agent",)),
        ToolSelection.unrestricted(),
        ToolSelection.forbid_all(),
    ])
    def test_없거나_닫히지_않은_턴은_거짓(self, selection: ToolSelection) -> None:
        # 열린 턴에는 profile.agents 가 안 넘어가 lookup, analyst 가 없다
        # (engine/claude._agent_args 와 같은 기준).
        assert selection.can_delegate is False


# ---- 지침 ----------------------------------------------------------------


class TestFlowSectionDelegation:
    def test_위임_가능이면_Agent_위임_안내가_있다(self) -> None:
        text = FlowSection().render(_ctx(delegation=True))
        assert 'Agent(subagent_type="lookup")' in text
        assert 'Agent(subagent_type="analyst")' in text
        for item in ("범위", "목표", "금지 조건", "반환 형식"):
            assert item in text

    def test_기본값은_위임_안내가_없고_나머지_절은_그대로다(self) -> None:
        text = FlowSection().render(_ctx())
        for word in ("subagent_type", "Agent", "lookup", "analyst", "반환 형식", "<<"):
            assert word not in text
        for word in ("1. 분석", "2. 계획", "3. 병렬 실행", "4. 다듬기", "[작업 순서]", "[계획 갱신]"):
            assert word in text
        assert "도구 호출을 묶어 부른다" in text

    def test_위임_안내가_있어도_60줄_이하다(self) -> None:
        assert len(FlowSection().render(_ctx(delegation=True)).splitlines()) <= 60


# ---- 훅 -----------------------------------------------------------------


def _write_plan(transcript: str, **fields: Any) -> None:
    import time
    ps.plan_path(transcript).write_text(
        json.dumps({"finished_at": time.time(), "status": "ok", **fields}), encoding="utf-8")


class TestPlanGateHook:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("PLAN_GATE", "3")

    def _call(self, tmp_path: Path) -> dict[str, Any]:
        call = {"transcript_path": str(tmp_path / "s.jsonl"), "tool_use_id": "t1", "tool_name": "Read"}
        _write_plan(call["transcript_path"], **_PLAN)
        return call

    def test_환경변수가_없으면_위임_문구가_없다(self, tmp_path: Path) -> None:
        verdict, context = pg.decide(self._call(tmp_path))
        assert verdict == "inject"
        assert "Agent" not in context and "위임" not in context and "subagent_type" not in context
        assert "1. 목표 : 상점 목록" in context and "훅을 언급하지 않는다" in context

    def test_환경변수가_있으면_위임_문구가_있다(self, tmp_path: Path, monkeypatch: Any) -> None:
        monkeypatch.setenv(DELEGATION_ENV, "1")
        _, context = pg.decide(self._call(tmp_path))
        assert "위임 : lookup" in context and 'Agent(subagent_type="<위임>")' in context

    @pytest.mark.parametrize("value", ["", "0", " 0 "])
    def test_0_이나_빈값은_없음이다(self, tmp_path: Path, monkeypatch: Any, value: str) -> None:
        monkeypatch.setenv(DELEGATION_ENV, value)
        _, context = pg.decide(self._call(tmp_path))
        assert "Agent" not in context


class TestPlanStartPlanner:
    def test_기본값은_작업자를_고를_수_없다(self) -> None:
        enum = ps.schema(False)["properties"]["branches"]["items"]["properties"]["delegate"]["enum"]
        assert enum == ["none"]
        prompt = ps.planner_prompt(False)
        assert "lookup" not in prompt and "analyst" not in prompt
        assert "5why" in prompt and "short 가 아니다" in prompt

    def test_위임_가능이면_작업자가_enum_과_지시문에_있다(self) -> None:
        enum = ps.schema(True)["properties"]["branches"]["items"]["properties"]["delegate"]["enum"]
        assert enum == ["none", "lookup", "analyst"]
        assert "lookup" in ps.planner_prompt(True)

    def test_환경변수로_고른다(self) -> None:
        assert ps.delegation_enabled({DELEGATION_ENV: "1"}) is True
        assert ps.delegation_enabled({}) is False

    def test_모듈_상수는_위임_없음이다(self) -> None:
        assert ps.SCHEMA == ps.schema(False) and ps.PLANNER_PROMPT == ps.planner_prompt(False)

    def test_계획기_호출이_환경변수대로_스키마를_쓴다(self, monkeypatch: Any) -> None:
        seen: list[tuple[list[str], str]] = []

        class FakeProc:
            pid = 1
            returncode = 0

            def communicate(self, input: str = "", timeout: float = 0) -> tuple[str, str]:
                seen.append((self.cmd, input))
                return "{}", ""

        def popen(cmd: list[str], **_: Any) -> Any:
            proc = FakeProc()
            proc.cmd = cmd  # type: ignore[attr-defined]
            return proc

        monkeypatch.setattr(subprocess, "Popen", popen)
        ps._call_planner("요청", "haiku", 1.0)
        monkeypatch.setenv(DELEGATION_ENV, "1")
        ps._call_planner("요청", "haiku", 1.0)
        off_cmd, off_in = seen[0]
        on_cmd, on_in = seen[1]
        assert "lookup" not in off_cmd[off_cmd.index("--json-schema") + 1] and "lookup" not in off_in
        assert "lookup" in on_cmd[on_cmd.index("--json-schema") + 1] and "lookup" in on_in


class TestSerialReadGuardReason:
    def test_기본값은_Agent_안내가_없다(self) -> None:
        reason = srg.build_reason(6, delegation=False)
        assert reason.startswith(srg.MARKER) and "[계획 갱신]" in reason
        assert "Agent" not in reason and "subagent_type" not in reason
        assert "같은 호출을 그대로 다시 불러라" in reason

    def test_위임_가능이면_Agent_안내가_있다(self) -> None:
        reason = srg.build_reason(6, delegation=True)
        assert 'Agent(subagent_type="lookup")' in reason
        assert "같은 호출을 그대로 다시 불러라" in reason

    @pytest.mark.parametrize(("env", "expected"), [(None, False), ("1", True)])
    def test_훅_출력이_환경변수를_따른다(
        self, monkeypatch: Any, capsys: Any, env: str | None, expected: bool,
    ) -> None:
        if env:
            monkeypatch.setenv(DELEGATION_ENV, env)
        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        monkeypatch.setattr(srg, "decide", lambda data: ("deny", 6))
        assert srg.main([]) == 0
        out = json.loads(capsys.readouterr().out)
        assert ("Agent(" in out["hookSpecificOutput"]["permissionDecisionReason"]) is expected


class TestParallelNudgeMessage:
    def test_기본값은_Agent_안내가_없다(self) -> None:
        text = pn.build_message(3, delegation=False)
        assert "Agent" not in text and "3번" in text
        assert "앞 결과가 있어야 다음을 정할 수 있는 조회만 순서대로 둔다" in text

    def test_위임_가능이면_Agent_안내가_있다(self) -> None:
        assert "Agent" in pn.build_message(3, delegation=True)


# ---- 파이프라인 -----------------------------------------------------------


OWNER_TOOLS_WITH_AGENT = ("Bash", "Agent", "Read")
OWNER_TOOLS_WITHOUT_AGENT = ("Bash", "Read")


def _run_owner_turn(tmp_path: Path, owner_tools: tuple[str, ...]) -> tuple[Any, Any]:
    from test_pipeline import FakeComposer, build_pipeline, make_ctx, ok_response

    composer = FakeComposer()
    policy = ToolPolicy(base_tools=("Read",), owner_tools=owner_tools)
    pipeline, deps = build_pipeline(
        responses=[ok_response()], tmp_path=tmp_path, tool_policy=policy, composer=composer,
    )
    pipeline._plan_gate_responses = 3
    pipeline.handle(make_ctx(channel="C1", user="UOWNER", ts="1700000001.000100",
                             thread_ts="1700000001.000100"))
    return deps["runner"].calls[0], composer


class TestPipelineDelegation:
    def test_Agent_가_있는_턴은_환경과_지침_문맥에_위임을_싣는다(self, tmp_path: Path) -> None:
        request, composer = _run_owner_turn(tmp_path, OWNER_TOOLS_WITH_AGENT)
        assert request.engine_env.get(DELEGATION_ENV) == "1"
        assert composer.contexts[-1].delegation is True

    def test_Agent_가_없는_턴은_싣지_않는다(self, tmp_path: Path) -> None:
        request, composer = _run_owner_turn(tmp_path, OWNER_TOOLS_WITHOUT_AGENT)
        assert DELEGATION_ENV not in request.engine_env
        assert composer.contexts[-1].delegation is False
        # 흐름 자체(PLAN_GATE)는 위임과 무관하게 켜진다
        assert request.engine_env.get("PLAN_GATE") == "3"

    def test_환경_허용목록에_있다(self) -> None:
        assert DELEGATION_ENV in ClaudeEnvironmentPolicy.ADDITIONAL_ALLOWED_VARS
        assert pg.AGENT_DELEGATION_ENV == ps.AGENT_DELEGATION_ENV == DELEGATION_ENV
        assert pn.AGENT_DELEGATION_ENV == DELEGATION_ENV


class TestFingerprint:
    def test_위임_여부가_지문에_들어간다(self, tmp_path: Path) -> None:
        from test_session_prompt_fingerprint import Test지문안정성

        helper = Test지문안정성()
        composer, _prompts, app = helper._composer(tmp_path)
        kwargs = {"channel_mode": "default", "turn_tier": "owner"}
        off = helper._ctx(helper.OWNER, **kwargs)
        on = helper._ctx(helper.OWNER, delegation=True, **kwargs)
        assert composer.instruction_fingerprint(off) != composer.instruction_fingerprint(on)
        assert composer.instruction_fingerprint(on) == composer.instruction_fingerprint(
            helper._ctx(helper.OWNER, delegation=True, thread_ts="9.9", prompt="다른 요청", **kwargs))
        app.close()
