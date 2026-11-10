"""기본 처리 흐름(W4) 시험 : FlowSection, guard/pretool/plan_gate, 계획 블록 제거,
serial_read_guard 거부 문구, 파이프라인의 PLAN_GATE 설정."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from slack_cli_agent.auth.principal import Principal, TrustLevel, TurnTier
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.markers import strip_control_tags, strip_plan_blocks
from slack_cli_agent.engine.claude import guard_hook_settings
from slack_cli_agent.engine.environment import ClaudeEnvironmentPolicy
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
        assert "[계획]" in text and "[계획 갱신]" in text
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


# ---- plan_gate ---------------------------------------------------------


def _assistant(mid: str, *, text: str = "", uses: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Claude Code 기록 모양 : 한 응답의 text 와 tool_use 가 같은 message.id 의 별도 줄이다."""
    lines: list[dict[str, Any]] = []
    if text:
        lines.append({"type": "assistant", "message": {"id": mid, "content": [{"type": "text", "text": text}]}})
    for use in uses:
        lines.append({"type": "assistant", "message": {"id": mid, "content": [
            {"type": "tool_use", "id": use, "name": "Read", "input": {"file_path": f"/{use}"}},
        ]}})
    return lines


def _result(use: str, text: str = "ok") -> dict[str, Any]:
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": use, "content": text}]}}


def _human(text: str = "조사해 줘") -> dict[str, Any]:
    return {"type": "user", "message": {"content": text}}


def _step(i: int, text: str = "") -> list[dict[str, Any]]:
    return [*_assistant(f"m{i}", text=text, uses=(f"t{i}",)), _result(f"t{i}")]


def _judge(records: list[dict[str, Any]], use: str, threshold: int = 3) -> str:
    return pg.judge(pg.responses_since_prompt(records), use, threshold)[0]


class TestPlanGateJudge:
    def test_응답_2개로_끝나는_요청은_통과한다(self) -> None:
        records = [_human(), *_step(0), *_assistant("m1", uses=("t1",))]
        assert _judge(records, "t1") == "early"

    def test_계획_없이_3번째_응답은_거부한다(self) -> None:
        records = [_human(), *_step(0), *_step(1), *_assistant("m2", uses=("t2",))]
        assert pg.judge(pg.responses_since_prompt(records), "t2") == ("deny", 3)

    def test_같은_응답_본문에_계획이_있으면_통과한다(self) -> None:
        records = [_human(), *_step(0), *_step(1), *_assistant("m2", text="[계획]\n1. 목표 : x", uses=("t2",))]
        assert _judge(records, "t2") == "same_response_plan"

    def test_tool_use_줄이_아직_없어도_앞선_본문을_이_응답으로_본다(self) -> None:
        records = [_human(), *_step(0), *_step(1), *_assistant("m2", text="[계획]\n1. 목표 : x")]
        assert _judge(records, "t2") == "same_response_plan"

    def test_첫_응답에_적은_계획이면_이후_통과한다(self) -> None:
        records = [_human(), *_step(0, text="[계획]\n1. a\n2. b"), *_step(1), *_step(2),
                   *_assistant("m3", uses=("t3",))]
        assert _judge(records, "t3") == "planned"

    def test_계획_갱신도_계획으로_본다(self) -> None:
        records = [_human(), *_step(0, text="[계획 갱신]\n3. c"), *_step(1), *_assistant("m2", uses=("t2",))]
        assert _judge(records, "t2") == "planned"

    def test_거부_뒤_계획을_적고_같은_호출을_다시_부르면_통과한다(self) -> None:
        denied = pg.REASON.format(count=3)
        records = [_human(), *_step(0), *_step(1), *_assistant("m2", uses=("t2",)), _result("t2", denied),
                   *_assistant("m3", text="[계획]\n1. 목표 : x", uses=("t3",))]
        assert _judge(records, "t3") == "same_response_plan"

    def test_계획_없이_다시_부르면_또_거부한다(self) -> None:
        denied = pg.REASON.format(count=3)
        records = [_human(), *_step(0), *_step(1), *_assistant("m2", uses=("t2",)), _result("t2", denied),
                   *_assistant("m3", uses=("t3",))]
        assert _judge(records, "t3") == "deny"

    def test_거부가_상한에_닿으면_더_묻지_않는다(self) -> None:
        denied = pg.REASON.format(count=3)
        records = [_human(), *_step(0), *_step(1)]
        for i in (2, 3):
            records += [*_assistant(f"m{i}", uses=(f"t{i}",)), _result(f"t{i}", denied)]
        records += _assistant("m4", uses=("t4",))
        assert _judge(records, "t4") == "gave_up"

    def test_사람_입력이_요청을_나눈다(self) -> None:
        records = [_human(), *_step(0), *_step(1), *_step(2), _human("다음"), *_assistant("m3", uses=("t3",))]
        assert _judge(records, "t3") == "early"

    def test_Stop_훅_피드백은_요청을_나누지_않는다(self) -> None:
        meta = {"type": "user", "isMeta": True, "message": {"content": "Stop hook feedback:\n근거 없음"}}
        records = [_human(), *_step(0, text="[계획]\n1. a"), meta, *_step(1), *_assistant("m2", uses=("t2",))]
        assert _judge(records, "t2") == "planned"

    def test_문턱값을_바꿀_수_있다(self) -> None:
        records = [_human(), *_step(0), *_step(1), *_assistant("m2", uses=("t2",))]
        assert _judge(records, "t2", threshold=4) == "early"

    def test_환경값_해석(self) -> None:
        assert pg.threshold_from_env({}) == 0
        assert pg.threshold_from_env({"PLAN_GATE": "3"}) == 3
        assert pg.threshold_from_env({"PLAN_GATE": "x"}) == 0


def _run_hook(path: Path, use: str, env_value: str | None) -> tuple[dict[str, Any] | None, int]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("PLAN_GATE", None)
    if env_value is not None:
        env["PLAN_GATE"] = env_value
    payload = {"hook_event_name": "PreToolUse", "transcript_path": str(path), "tool_use_id": use,
               "tool_name": "Read", "tool_input": {"file_path": f"/{use}"}}
    proc = subprocess.run([sys.executable, str(pg.HOOK_SCRIPT)], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=15)
    out = proc.stdout.strip()
    return (json.loads(out) if out else None), proc.returncode


class TestPlanGateHook:
    def _write(self, path: Path) -> None:
        records = [_human(), *_step(0), *_step(1), *_assistant("m2", uses=("t2",))]
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")

    def test_PLAN_GATE_가_있으면_거부_JSON_을_낸다(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        self._write(path)
        result, code = _run_hook(path, "t2", "3")
        assert code == 0
        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert out["permissionDecisionReason"].startswith(pg.MARKER)
        assert "[계획]" in out["permissionDecisionReason"]

    def test_PLAN_GATE_가_없으면_아무것도_하지_않는다(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        self._write(path)
        assert _run_hook(path, "t2", None) == (None, 0)

    def test_깨진_입력에도_실패하지_않는다(self) -> None:
        proc = subprocess.run([sys.executable, str(pg.HOOK_SCRIPT)], input="not json",
                              capture_output=True, text=True, timeout=15,
                              env={**os.environ, "PLAN_GATE": "3"})
        assert proc.returncode == 0 and proc.stdout == ""

    def test_엔진_기본_훅에_등록된다(self) -> None:
        star = [g for g in guard_hook_settings()["hooks"]["PreToolUse"] if g["matcher"] == "*"]
        commands = [h["command"] for h in star[0]["hooks"]]
        assert any("plan_gate.py" in c for c in commands)

    def test_엔진_환경_허용목록에_있다(self) -> None:
        assert pg.PLAN_GATE_ENV in ClaudeEnvironmentPolicy.ADDITIONAL_ALLOWED_VARS


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
