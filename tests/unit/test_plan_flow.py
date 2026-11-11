"""기본 처리 흐름(W4) 시험 : FlowSection, guard/pretool/plan_gate, 계획 블록 제거,
serial_read_guard 거부 문구, 파이프라인의 PLAN_GATE 설정."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from slack_cli_agent.auth.principal import Principal, TrustLevel, TurnTier
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.markers import strip_control_tags, strip_plan_blocks
from slack_cli_agent.engine.claude import guard_hook_settings
from slack_cli_agent.engine.environment import ClaudeEnvironmentPolicy
from slack_cli_agent.guard import plan_start as ps
from slack_cli_agent.guard.pretool import plan_gate as pg
from slack_cli_agent.guard.pretool import serial_read_guard as srg
from slack_cli_agent.prompt.library import PromptLibrary
from slack_cli_agent.prompt.sections import (
    FLOW_TIERS,
    CompositionContext,
    FlowSection,
    flow_applies,
)

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
_PRINCIPAL = Principal(user_id="U1", channel="C1", trust=TrustLevel.GENERAL, is_direct_message=False)


def _ctx(**overrides: object) -> CompositionContext:
    base: dict[str, object] = {
        "principal": _PRINCIPAL,
        "library": PromptLibrary(Path("/does/not/exist")),
        "turn_tier": TurnTier.OWNER.value,
    }
    base.update(overrides)
    return CompositionContext(**base)  # type: ignore[arg-type]


class TestFlowSection:
    @pytest.mark.parametrize("tier", ["owner_trusted", "owner", "delegated", "operator", "operator_full"])
    def test_Agent_가_열린_티어에_들어간다(self, tier: str) -> None:
        assert FlowSection().applies_to(_ctx(turn_tier=tier)) is True

    @pytest.mark.parametrize("tier", ["read", "bot", "airflow", "readonly", ""])
    def test_Agent_가_닫힌_티어에는_없다(self, tier: str) -> None:
        assert FlowSection().applies_to(_ctx(turn_tier=tier)) is False

    @pytest.mark.parametrize("flag", ["postmortem", "debug_trace", "format_review", "watch_check"])
    def test_돌아보는_턴에는_없다(self, flag: str) -> None:
        assert FlowSection().applies_to(_ctx(**{flag: True})) is False

    def test_잡담_채널에는_없다(self) -> None:
        assert FlowSection().applies_to(_ctx(chat_level="active")) is False
        assert FlowSection().applies_to(_ctx(channel_mode="chat")) is False

    def test_프로필이_끄면_없다(self) -> None:
        assert FlowSection(enabled=False).applies_to(_ctx()) is False

    def test_프로필_설정_기본값은_켬이다(self) -> None:
        settings = RuntimeSettings()
        assert settings.plan_flow is True
        assert settings.plan_gate_responses == 3
        assert RuntimeSettings().override({"plan_flow": False}).plan_flow is False

    def test_네_단계와_계획_형식과_위임을_담는다(self) -> None:
        text = FlowSection().render(_ctx())
        for word in ("1. 분석", "2. 계획", "3. 병렬 실행", "4. 다듬기", "단답형", "갈래형", "탐색형"):
            assert word in text
        assert "[작업 순서]" in text and "[계획 갱신]" in text
        assert "계획 확인 훅이 호출을 거부한다" not in text
        assert "갈래는 2-5개" in text
        assert 'Agent(subagent_type="lookup")' in text
        assert 'Agent(subagent_type="analyst")' in text
        for item in ("범위", "목표", "금지 조건", "반환 형식"):
            assert item in text

    def test_60줄_이하다(self) -> None:
        assert len(FlowSection().render(_ctx()).splitlines()) <= 60

    def test_티어_집합과_판정이_같다(self) -> None:
        assert {"owner", "operator", "delegated"} <= set(FLOW_TIERS)
        assert flow_applies("owner", chat=False) is True
        assert flow_applies("owner", chat=True) is False
        assert flow_applies("read", chat=False) is False


# ---- plan_start / plan_gate ------------------------------------------


_PLAN = {
    "type": "branch",
    "branches": [
        {"goal": "어제 주문 수", "source": "DATAMART.ORDERS", "depends_on": [], "delegate": "none",
         "returns": "수치"},
        {"goal": "상점 목록", "source": "stores.csv", "depends_on": [], "delegate": "lookup",
         "returns": "목록"},
    ],
}


def _cli_result(plan: dict[str, Any] | None, **extra: Any) -> str:
    return json.dumps({"is_error": False, "structured_output": plan, "total_cost_usd": 0.001, **extra})


class TestPlannerParse:
    def test_정상_출력에서_계획을_꺼낸다(self) -> None:
        status, plan = ps.parse(_cli_result(_PLAN))
        assert status == "ok" and plan is not None
        assert [b["goal"] for b in plan["branches"]] == ["어제 주문 수", "상점 목록"]

    def test_깨진_출력은_bad_output_이다(self) -> None:
        assert ps.parse("not json") == ("bad_output", None)
        assert ps.parse(_cli_result({"type": "모름", "branches": []})) == ("bad_output", None)
        assert ps.parse(_cli_result(None)) == ("bad_output", None)

    def test_CLI_오류는_error_cli_다(self) -> None:
        assert ps.parse(json.dumps({"is_error": True}))[0] == "error:cli"


class TestPlannerRun:
    def _prepare(self, tmp_path: Path) -> str:
        transcript = str(tmp_path / "s.jsonl")
        ps.prompt_path(transcript).write_text("요청", encoding="utf-8")
        return transcript

    def test_성공하면_계획_파일에_ok_로_쓴다(self, tmp_path: Path) -> None:
        transcript = self._prepare(tmp_path)
        ok = subprocess.CompletedProcess([], 0, _cli_result(_PLAN), "")
        record = ps.run(transcript, caller=lambda *_: ok)
        saved = json.loads(ps.plan_path(transcript).read_text(encoding="utf-8"))
        assert record["status"] == saved["status"] == "ok"
        assert saved["type"] == "branch" and len(saved["branches"]) == 2
        assert not ps.prompt_path(transcript).exists()

    def test_시간_초과는_timeout_으로_남기고_계획을_만들지_않는다(self, tmp_path: Path) -> None:
        transcript = self._prepare(tmp_path)

        def slow(*_: Any) -> Any:
            raise subprocess.TimeoutExpired("claude", 20)

        record = ps.run(transcript, caller=slow)
        assert record["status"] == "timeout"
        assert "branches" not in json.loads(ps.plan_path(transcript).read_text(encoding="utf-8"))

    def test_깨진_JSON_은_bad_output_으로_남긴다(self, tmp_path: Path) -> None:
        transcript = self._prepare(tmp_path)
        bad = subprocess.CompletedProcess([], 0, "{oops", "")
        assert ps.run(transcript, caller=lambda *_: bad)["status"] == "bad_output"


class TestPlannerStart:
    def _data(self, tmp_path: Path, **extra: Any) -> dict[str, Any]:
        return {"transcript_path": str(tmp_path / "s.jsonl"), "prompt": "조회해 줘", **extra}

    def test_켜진_턴에서_앞_요청_파일을_지우고_분리_실행한다(self, tmp_path: Path, monkeypatch: Any) -> None:
        monkeypatch.setenv("PLAN_GATE", "3")
        monkeypatch.delenv("SCA_PLAN_NESTED", raising=False)
        monkeypatch.delenv("SCA_VERIFY_NESTED", raising=False)
        data = self._data(tmp_path)
        ps.injected_path(data["transcript_path"]).write_text("1")
        calls: list[tuple[list[str], dict[str, Any]]] = []
        assert ps.start(data, spawn=lambda cmd, **kw: calls.append((cmd, kw))) == "started"
        assert not ps.injected_path(data["transcript_path"]).exists()
        assert json.loads(ps.plan_path(data["transcript_path"]).read_text())["status"] == "pending"
        assert ps.prompt_path(data["transcript_path"]).read_text() == "조회해 줘"
        cmd, kw = calls[0]
        assert cmd[-2:] == ["--run", data["transcript_path"]]
        assert kw["start_new_session"] is True and kw["env"]["SCA_PLAN_NESTED"] == "1"

    @pytest.mark.parametrize("env", [{}, {"PLAN_GATE": "0"}, {"PLAN_GATE": "3", "SCA_PLAN_NESTED": "1"},
                                     {"PLAN_GATE": "3", "SCA_VERIFY_NESTED": "1"}])
    def test_꺼졌거나_중첩_호출이면_실행하지_않는다(self, tmp_path: Path, monkeypatch: Any,
                                       env: dict[str, str]) -> None:
        for key in ("PLAN_GATE", "SCA_PLAN_NESTED", "SCA_VERIFY_NESTED"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        assert ps.start(self._data(tmp_path), spawn=lambda *a, **k: pytest.fail("spawned")) != "started"

    def test_훅은_출력_없이_바로_끝난다(self, tmp_path: Path) -> None:
        env = {**os.environ, "PYTHONPATH": str(SRC_ROOT), "PLAN_GATE": "0"}
        proc = subprocess.run([sys.executable, str(ps.HOOK_SCRIPT)], input=json.dumps(self._data(tmp_path)),
                              capture_output=True, text=True, env=env, timeout=15)
        assert proc.returncode == 0 and proc.stdout == ""


def _write_plan(transcript: str, **plan: Any) -> None:
    ps.plan_path(transcript).write_text(json.dumps({"finished_at": time.time(), **plan}), encoding="utf-8")


class TestPlanGateInject:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("PLAN_GATE", "3")

    def _call(self, tmp_path: Path, **extra: Any) -> dict[str, Any]:
        return {"transcript_path": str(tmp_path / "s.jsonl"), "tool_use_id": "t1", "tool_name": "Read", **extra}

    def test_계획이_준비되면_한_번만_주입한다(self, tmp_path: Path) -> None:
        call = self._call(tmp_path)
        _write_plan(call["transcript_path"], status="ok", **_PLAN)
        verdict, context = pg.decide(call)
        assert verdict == "inject"
        assert context.startswith(pg.INJECT_HEAD)
        assert "1. 목표 : 어제 주문 수" in context and "위임 : lookup" in context
        assert "훅을 언급하지 않는다" in context
        assert pg.decide(call) == ("already", "")

    def test_계획이_없거나_진행_중이면_아무것도_하지_않는다(self, tmp_path: Path) -> None:
        call = self._call(tmp_path)
        assert pg.decide(call)[0] == "no_plan"
        _write_plan(call["transcript_path"], status="pending")
        assert pg.decide(call)[0] == "pending"

    def test_실패한_계획과_단답형은_주입하지_않는다(self, tmp_path: Path) -> None:
        call = self._call(tmp_path)
        _write_plan(call["transcript_path"], status="timeout")
        assert pg.decide(call)[0] == "plan_failed"
        _write_plan(call["transcript_path"], status="ok", type="short", branches=[])
        assert pg.decide(call)[0] == "short"

    def test_오래된_계획은_무시한다(self, tmp_path: Path) -> None:
        call = self._call(tmp_path)
        ps.plan_path(call["transcript_path"]).write_text(
            json.dumps({"status": "ok", "finished_at": time.time() - 7200, **_PLAN}), encoding="utf-8")
        assert pg.decide(call)[0] == "no_plan"

    def test_작업자_호출은_건너뛴다(self, tmp_path: Path) -> None:
        call = self._call(tmp_path, agent_id="a1")
        _write_plan(call["transcript_path"], status="ok", **_PLAN)
        assert pg.decide(call)[0] == "subagent"

    def test_꺼져_있으면_아무것도_하지_않는다(self, tmp_path: Path, monkeypatch: Any) -> None:
        monkeypatch.delenv("PLAN_GATE")
        call = self._call(tmp_path)
        _write_plan(call["transcript_path"], status="ok", **_PLAN)
        assert pg.decide(call)[0] == "off"

    def test_환경값_해석(self) -> None:
        assert pg.threshold_from_env({}) == 0
        assert pg.threshold_from_env({"PLAN_GATE": ""}) == 0
        assert pg.threshold_from_env({"PLAN_GATE": "x"}) == 0
        assert pg.threshold_from_env({"PLAN_GATE": "3"}) == 3


def _run_hook(transcript: str, env_value: str | None) -> tuple[dict[str, Any] | None, int]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("PLAN_GATE", None)
    if env_value is not None:
        env["PLAN_GATE"] = env_value
    payload = {"hook_event_name": "PreToolUse", "transcript_path": transcript, "tool_use_id": "t1",
               "tool_name": "Read", "tool_input": {"file_path": "/x"}}
    proc = subprocess.run([sys.executable, str(pg.HOOK_SCRIPT)], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=15)
    out = proc.stdout.strip()
    return (json.loads(out) if out else None), proc.returncode


class TestPlanGateHook:
    def test_주입은_additionalContext_로만_내고_거부하지_않는다(self, tmp_path: Path) -> None:
        transcript = str(tmp_path / "s.jsonl")
        _write_plan(transcript, status="ok", **_PLAN)
        result, code = _run_hook(transcript, "3")
        assert code == 0 and result is not None
        out = result["hookSpecificOutput"]
        assert out["hookEventName"] == "PreToolUse"
        assert "permissionDecision" not in out
        assert out["additionalContext"].startswith(pg.INJECT_HEAD)

    def test_PLAN_GATE_가_없으면_아무것도_하지_않는다(self, tmp_path: Path) -> None:
        transcript = str(tmp_path / "s.jsonl")
        _write_plan(transcript, status="ok", **_PLAN)
        assert _run_hook(transcript, None) == (None, 0)

    def test_깨진_입력에도_실패하지_않는다(self) -> None:
        proc = subprocess.run([sys.executable, str(pg.HOOK_SCRIPT)], input="not json",
                              capture_output=True, text=True, timeout=15,
                              env={**os.environ, "PLAN_GATE": "3"})
        assert proc.returncode == 0 and proc.stdout == ""

    def test_엔진_기본_훅에_등록된다(self) -> None:
        hooks = guard_hook_settings()["hooks"]
        star = [g for g in hooks["PreToolUse"] if g["matcher"] == "*"]
        assert any("plan_gate.py" in h["command"] for h in star[0]["hooks"])
        assert any("plan_start.py" in h["command"] for g in hooks["UserPromptSubmit"] for h in g["hooks"])

    def test_엔진_환경_허용목록에_있다(self) -> None:
        assert pg.PLAN_GATE_ENV in ClaudeEnvironmentPolicy.ADDITIONAL_ALLOWED_VARS
        assert ps.PLAN_MODEL_ENV in ClaudeEnvironmentPolicy.ADDITIONAL_ALLOWED_VARS


# ---- serial_read_guard 문구 ---------------------------------------------


def test_연속_조회_거부_문구가_계획_갱신과_위임을_안내한다() -> None:
    reason = srg.REASON.format(run=6)
    assert reason.startswith(srg.MARKER)
    assert "[계획 갱신]" in reason
    assert 'Agent(subagent_type="lookup")' in reason
    assert "같은 호출을 그대로 다시 불러라" in reason


# ---- 계획 블록이 게시되지 않는다 ------------------------------------------


class TestPlanBlockStrip:
    def test_최종_본문의_계획_블록을_뗀다(self) -> None:
        body = "[계획]\n1. 목표 : a / 원천 : b\n2. 목표 : c\n\n결론 : 3건이다."
        text, dropped = strip_control_tags(body)
        assert text == "결론 : 3건이다."
        assert dropped == ["[계획]"]

    def test_계획_갱신_블록도_뗀다(self) -> None:
        text, dropped = strip_plan_blocks("앞 문단\n[계획 갱신]\n- 3. 목표 : x\n뒷 문단")
        assert text == "앞 문단\n뒷 문단"
        assert dropped == ["[계획 갱신]"]

    def test_코드블록_안의_예시는_남긴다(self) -> None:
        body = "형식 예시\n```\n[계획]\n1. 목표 : x\n```"
        assert strip_control_tags(body) == (body, [])

    def test_문장_속_언급은_남긴다(self) -> None:
        body = "계획 블록은 `[계획]` 으로 시작한다."
        assert strip_control_tags(body) == (body, [])

    def test_다른_표식과_함께_뗀다(self) -> None:
        text, dropped = strip_control_tags("[계획]\n1. a\n본문\n[[ASK]]")
        assert text == "본문"
        assert dropped == ["[계획]", "[[ASK]]"]


# ---- 파이프라인이 PLAN_GATE 를 싣는다 --------------------------------------


class TestPipelinePlanGateEnv:
    def _pipeline(self, tmp_path: Path, responses: int) -> Any:
        from test_pipeline import build_pipeline, ok_response

        pipeline, deps = build_pipeline(responses=[ok_response()], tmp_path=tmp_path)
        pipeline._plan_gate_responses = responses
        return pipeline, deps

    def test_흐름이_적용되는_티어에만_싣는다(self, tmp_path: Path) -> None:
        from slack_cli_agent.config.channel import ChannelConfig

        pipeline, _ = self._pipeline(tmp_path, 3)
        work = ChannelConfig("C1")
        assert pipeline._plan_gate_env("owner", work) == {"PLAN_GATE": "3"}
        assert pipeline._plan_gate_env("operator", None) == {"PLAN_GATE": "3"}
        assert pipeline._plan_gate_env("read", work) == {}
        assert pipeline._plan_gate_env("bot", work) == {}
        assert pipeline._plan_gate_env("owner", ChannelConfig("C1", chat="active")) == {}
        assert pipeline._plan_gate_env("owner", ChannelConfig("C1", mode="chat")) == {}

    def test_0_이면_싣지_않는다(self, tmp_path: Path) -> None:
        pipeline, _ = self._pipeline(tmp_path, 0)
        assert pipeline._plan_gate_env("owner", None) == {}

    @pytest.mark.parametrize(("user", "expected"), [("UOWNER", True), ("U2", False)])
    def test_요청_엔진_환경에_반영된다(self, tmp_path: Path, user: str, expected: bool) -> None:
        from test_pipeline import make_ctx

        pipeline, deps = self._pipeline(tmp_path, 3)
        pipeline.handle(make_ctx(channel="C1", user=user))
        call = deps["runner"].calls[0]
        assert ("PLAN_GATE" in call.engine_env) is expected
