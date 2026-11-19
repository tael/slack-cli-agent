"""N0: 계획 앞 입력 정제(SCA_PLAN_REFINE)와 계획 지침 보정(SCA_PLAN_SOURCES). 꺼짐이 기본."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest
from test_pipeline import build_pipeline
from test_plan_intake import BASE_SHA, BRANCH_PLAN, INTAKE, _sha

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.engine.environment import ClaudeEnvironmentPolicy
from slack_cli_agent.engine.tool_selection import ToolSelection
from slack_cli_agent.guard import plan_start as ps

REFINE_OK: dict[str, Any] = {
    "refined": "2026-11-18 21시대 신지 응답 지연 3건의 원인을 확인해줘",
    "resolved": [{"from": "어제", "to": "2026-11-18", "basis": "현재 시각 2026-11-19"}],
    "unresolved": ["3시가 오전인지 오후인지"],
}
NOW = datetime(2026, 11, 18, 16, 40, tzinfo=UTC)  # 2026-11-19 01:40 KST


def _cli(plan: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    out = json.dumps({"is_error": False, "structured_output": plan, "total_cost_usd": 0.001})
    return subprocess.CompletedProcess([], 0, out, "")


class Calls:
    def __init__(self, plan: dict[str, Any] | None = None, refine: Any = None) -> None:
        self.plan = plan or {"type": "short", "branches": []}
        self.refine = refine
        self.planner_prompts: list[str] = []
        self.refine_prompts: list[str] = []

    def planner(self, prompt: str, model: str, timeout: float) -> subprocess.CompletedProcess[str]:
        self.planner_prompts.append(prompt)
        return _cli(self.plan)

    def refiner(self, prompt: str, model: str, timeout: float) -> subprocess.CompletedProcess[str]:
        self.refine_prompts.append(prompt)
        if isinstance(self.refine, Exception):
            raise self.refine
        return self.refine if isinstance(self.refine, subprocess.CompletedProcess) else _cli(self.refine)


def _run(tmp_path: Path, calls: Calls, request: str = "그거 왜 그래?", context: str = "") -> dict[str, Any]:
    transcript = str(tmp_path / "s.jsonl")
    ps.prompt_path(transcript).write_text(request, encoding="utf-8")
    if context:
        ps.context_path(transcript).write_text(context, encoding="utf-8")
    return ps.run(transcript, caller=calls.planner, refiner=calls.refiner, now=NOW)


class TestSwitchOffIsUnchanged:
    @pytest.mark.parametrize("delegation", [False, True])
    def test_꺼져_있으면_지침과_schema_가_변경_전_바이트와_같다(self, delegation: bool) -> None:
        prompt_sha, schema_sha = BASE_SHA[delegation]
        assert _sha(ps.planner_prompt(delegation, None, False, None, "")) == prompt_sha
        assert _sha(json.dumps(ps.schema(delegation, None, False, ""), sort_keys=True)) == schema_sha

    def test_꺼져_있으면_정제_호출도_기록도_없다(self, tmp_path: Path) -> None:
        calls = Calls(refine=REFINE_OK)
        record = _run(tmp_path, calls, "요청 원문")
        assert calls.refine_prompts == [] and calls.planner_prompts == ["요청 원문"]
        assert "refine" not in record and record["status"] == "ok"

    def test_알_수_없는_값은_꺼짐이다(self) -> None:
        for raw in ("", " ", "0", "on", "Separate1"):
            assert ps.refine_mode({ps.REFINE_ENV: raw}) == ""
        assert ps.refine_mode({}) == ""
        assert ps.refine_mode({ps.REFINE_ENV: " separate "}) == "separate"
        assert ps.refine_mode({ps.REFINE_ENV: "combined"}) == "combined"

    def test_직전_대화는_정제가_켜져야_계획_옆_파일에_쓴다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        transcript = str(tmp_path / "t.jsonl")
        Path(transcript).write_text(
            json.dumps({"type": "user", "message": {"content": "앞 질문"}}) + "\n", encoding="utf-8"
        )
        monkeypatch.setenv(ps.PLAN_GATE_ENV, "3")
        ps.start({"transcript_path": transcript, "prompt": "후속"}, spawn=lambda *a, **k: None)
        assert not ps.context_path(transcript).exists()
        monkeypatch.setenv(ps.REFINE_ENV, "separate")
        ps.start({"transcript_path": transcript, "prompt": "후속"}, spawn=lambda *a, **k: None)
        assert "앞 질문" in ps.context_path(transcript).read_text(encoding="utf-8")


class TestSeparate:
    def test_정제_결과가_계획_입력에_들어가고_기록된다(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ps.REFINE_ENV, "separate")
        calls = Calls(refine=REFINE_OK)
        record = _run(tmp_path, calls, "그거 왜 그래?", context="사용자: 어제 늦던데")
        prompt = calls.planner_prompts[0]
        assert REFINE_OK["refined"] in prompt
        assert "어제 → 2026-11-18" in prompt and "3시가 오전인지 오후인지" in prompt
        assert "그거 왜 그래?" in prompt
        refine = record["refine"]
        assert refine["mode"] == "separate" and refine["status"] == "ok"
        assert refine["refined"] == REFINE_OK["refined"] and refine["resolved"] == REFINE_OK["resolved"]
        assert refine["unresolved"] == REFINE_OK["unresolved"] and isinstance(refine["elapsed_sec"], float)
        assert record["status"] == "ok"

    def test_정제_입력에_현재_KST_시각과_직전_대화와_요청이_있다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(ps.REFINE_ENV, "separate")
        calls = Calls(refine=REFINE_OK)
        _run(tmp_path, calls, "그거 왜 그래?", context="사용자: 어제 늦던데")
        text = calls.refine_prompts[0]
        assert "2026-11-19 (목) 01:40 KST" in text
        assert "사용자: 어제 늦던데" in text and text.rstrip().endswith("그거 왜 그래?")

    def test_정제_지침은_근거_없는_추정과_대상어_재해석과_범위_변경을_금한다(self) -> None:
        text = ps.refine_prompt()
        assert "basis" in text and "추측" in text
        assert "대상어" in text and "치과 캘린더" in text
        assert "넓히거나 줄이지" in text

    @pytest.mark.parametrize("failure,status", [
        (subprocess.TimeoutExpired("claude", 1), "timeout"),
        (RuntimeError("boom"), "error:RuntimeError"),
        (subprocess.CompletedProcess([], 0, "not json", ""), "bad_output"),
        (subprocess.CompletedProcess([], 0, json.dumps({"is_error": True}), ""), "error:cli"),
        ({"refined": ""}, "bad_output"),
        ({"refined": "x", "resolved": "no", "unresolved": []}, "bad_output"),
    ])
    def test_정제가_실패해도_원문으로_계획을_이어간다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Any, status: str,
    ) -> None:
        monkeypatch.setenv(ps.REFINE_ENV, "separate")
        calls = Calls(plan=BRANCH_PLAN, refine=failure)
        record = _run(tmp_path, calls, "원문 요청")
        assert calls.planner_prompts == ["원문 요청"]
        assert record["status"] == "ok" and record["type"] == "branch"
        assert record["refine"]["status"] == status and "refined" not in record["refine"]

    def test_근거_없는_resolved_는_unresolved_로_옮긴다(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ps.REFINE_ENV, "separate")
        guess = {"from": "3시", "to": "15:00", "basis": " "}
        calls = Calls(refine={**REFINE_OK, "resolved": [*REFINE_OK["resolved"], guess]})
        record = _run(tmp_path, calls)
        assert record["refine"]["resolved"] == REFINE_OK["resolved"]
        assert "3시" in record["refine"]["unresolved"]

    def test_계획_지침에_추정은_assumed_로_넘기라고_적는다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(ps.REFINE_ENV, "separate")
        calls = Calls(refine=REFINE_OK)
        _run(tmp_path, calls)
        assert "assumed" in calls.planner_prompts[0]

    def test_계획과_같은_환경_변수로_모델을_고른다(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ps.REFINE_ENV, "separate")
        monkeypatch.setenv(ps.PLAN_MODEL_ENV, "sonnet")
        assert _run(tmp_path, Calls(refine=REFINE_OK))["model"] == "sonnet"


class TestCombined:
    def test_schema_에_정제_필드가_필수로_생기고_호출은_한_번이다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = ps.schema(False, None, False, "combined")
        for key in ps.REFINE_FIELDS:
            assert key in result["properties"] and key in result["required"]
        assert "refined" not in ps.schema(False, None, False, "separate")["properties"]
        monkeypatch.setenv(ps.REFINE_ENV, "combined")
        calls = Calls(plan={**BRANCH_PLAN, **REFINE_OK})
        record = _run(tmp_path, calls)
        assert calls.refine_prompts == [] and len(calls.planner_prompts) == 1
        assert record["refine"]["mode"] == "combined" and record["refine"]["status"] == "ok"
        assert record["refine"]["refined"] == REFINE_OK["refined"]
        assert not [key for key in ps.REFINE_FIELDS if key in record]
        assert record["type"] == "branch"

    def test_지침은_먼저_정제하라고_적고_요청_표식_앞에_온다(self) -> None:
        text = ps.planner_prompt(False, None, False, None, "combined")
        assert text.endswith("\n[요청]\n") and "[입력 정제]" in text and "refined" in text
        assert text.index("[입력 정제]") < text.rindex("[요청]")

    def test_정제_필드가_틀리면_그_필드만_버리고_계획은_유지한다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(ps.REFINE_ENV, "combined")
        record = _run(tmp_path, Calls(plan={**BRANCH_PLAN, "refined": 3, "resolved": [], "unresolved": []}))
        assert record["status"] == "ok" and len(record["branches"]) == 2
        assert record["refine"]["status"] == "bad_output" and "refined" not in record


class TestPlannerFixes:
    SOURCES: ClassVar[list[str]] = ["Read", "mcp__slack__search"]

    def test_출처_목록_해석(self) -> None:
        assert ps.sources_list({}) == []
        assert ps.sources_list({ps.SOURCES_ENV: "not json"}) == []
        assert ps.sources_list({ps.SOURCES_ENV: '{"a": 1}'}) == []
        assert ps.sources_list({ps.SOURCES_ENV: json.dumps(["a", "a", 3, " ", "b"])}) == ["a", "b"]

    def test_입력_점검이_켜지고_목록이_있으면_출처를_그_목록으로_제한한다(self) -> None:
        text = ps.planner_prompt(False, None, True, self.SOURCES)
        assert "mcp__slack__search" in text and "확인 필요" in text and "만들지 않는다" in text
        assert text.endswith("\n[요청]\n")

    def test_입력_점검이_꺼져_있으면_목록이_있어도_지침이_같다(self) -> None:
        assert ps.planner_prompt(False, None, False, self.SOURCES) == ps.planner_prompt(False)
        assert ps.planner_prompt(False, None, True, None) == ps.planner_prompt(False, None, True, [])

    def test_ack_규칙은_해석과_할_일만_허용한다(self) -> None:
        text = ps.planner_prompt(False, None, True)
        assert "공감" in text and "상담" in text and "요청에 없는 내용" in text

    def test_쓰기_요청은_대상이나_필수값이_불명이면_ask_와_missing_을_채우게_한다(self) -> None:
        text = ps.planner_prompt(False, None, True)
        assert "쓰기·전송·삭제" in text and "ask 는 비우지 않고" in text and "missing" in text

    def test_run_이_환경의_목록을_계획_호출_지침에_넘긴다(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}

        def fake_popen(cmd: list[str], **kwargs: Any) -> Any:
            class Proc:
                pid = 1
                returncode = 0

                def communicate(self, input: str | None = None, timeout: float | None = None) -> tuple[str, str]:  # noqa: A002
                    seen["input"] = input
                    return json.dumps({"structured_output": {"type": "short", "branches": []}}), ""

            seen["cmd"] = cmd
            return Proc()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        monkeypatch.setenv(ps.INTAKE_ENV, "1")
        monkeypatch.setenv(ps.SOURCES_ENV, json.dumps(self.SOURCES))
        ps._call_planner("요청", "haiku", 5)
        assert "mcp__slack__search" in seen["input"]
        monkeypatch.delenv(ps.SOURCES_ENV)
        ps._call_planner("요청", "haiku", 5)
        assert "mcp__slack__search" not in seen["input"]


class TestPipelineAndWiring:
    TOOLS = ToolSelection.allow(["Read", "mcp__slack__search"])

    def _pipeline(self, tmp_path: Path, **attrs: Any) -> Any:
        pipeline, _ = build_pipeline(responses=[], tmp_path=tmp_path)
        pipeline._plan_gate_responses = 3
        pipeline._plan_workflows = {}
        pipeline._plan_intake = False
        for key, value in attrs.items():
            setattr(pipeline, key, value)
        return pipeline

    def test_꺼져_있으면_환경이_지금과_같다(self, tmp_path: Path) -> None:
        pipeline = self._pipeline(tmp_path)
        assert pipeline._plan_gate_env("owner", None, self.TOOLS) == {"PLAN_GATE": "3"}
        assert pipeline._plan_gate_env("owner", None) == {"PLAN_GATE": "3"}

    @pytest.mark.parametrize("mode", ["separate", "combined"])
    def test_정제_모드가_환경에_실린다(self, tmp_path: Path, mode: str) -> None:
        pipeline = self._pipeline(tmp_path, _plan_refine=mode)
        assert pipeline._plan_gate_env("owner", None)[ps.REFINE_ENV] == mode

    def test_알_수_없는_모드는_싣지_않는다(self, tmp_path: Path) -> None:
        pipeline = self._pipeline(tmp_path, _plan_refine="bogus")
        assert ps.REFINE_ENV not in pipeline._plan_gate_env("owner", None)

    def test_출처_목록은_입력_점검이_켜졌을_때만_싣는다(self, tmp_path: Path) -> None:
        on = self._pipeline(tmp_path, _plan_intake=True)._plan_gate_env("owner", None, self.TOOLS)
        assert json.loads(on[ps.SOURCES_ENV]) == ["Read", "mcp__slack__search"]
        off = self._pipeline(tmp_path)._plan_gate_env("owner", None, self.TOOLS)
        assert ps.SOURCES_ENV not in off

    def test_도구_이름이_없으면_출처_목록을_싣지_않는다(self, tmp_path: Path) -> None:
        pipeline = self._pipeline(tmp_path, _plan_intake=True)
        env = pipeline._plan_gate_env("owner", None, ToolSelection.unrestricted())
        assert ps.SOURCES_ENV not in env

    def test_설정_기본값은_꺼짐이다(self) -> None:
        assert RuntimeSettings().plan_refine == ""

    def test_엔진_환경_허용목록에_있다(self) -> None:
        for name in (ps.REFINE_ENV, ps.SOURCES_ENV):
            assert name in ClaudeEnvironmentPolicy.ADDITIONAL_ALLOWED_VARS

    def test_입력_점검과_함께여도_정제_기록이_계획_필드를_가리지_않는다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(ps.REFINE_ENV, "separate")
        monkeypatch.setenv(ps.INTAKE_ENV, "1")
        record = _run(tmp_path, Calls(plan={**BRANCH_PLAN, **INTAKE}, refine=REFINE_OK))
        assert record["ack"] == INTAKE["ack"] and record["refine"]["status"] == "ok"


class TestPlannerContext:
    """Intake and combined refine judge ask/missing from the thread, so the planner gets prior turns."""

    def test_입력_점검만_켜져도_직전_대화를_계획_옆_파일에_쓴다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        transcript = str(tmp_path / "t.jsonl")
        Path(transcript).write_text(
            json.dumps({"type": "user", "message": {"content": "앞 질문"}}) + "\n", encoding="utf-8"
        )
        monkeypatch.setenv(ps.PLAN_GATE_ENV, "3")
        monkeypatch.setenv(ps.INTAKE_ENV, "1")
        ps.start({"transcript_path": transcript, "prompt": "후속"}, spawn=lambda *a, **k: None)
        assert "앞 질문" in ps.context_path(transcript).read_text(encoding="utf-8")

    def test_입력_점검이_켜지면_계획_입력에_직전_대화가_붙는다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(ps.INTAKE_ENV, "1")
        calls = Calls()
        _run(tmp_path, calls, "그거 왜 그래?", context="사용자: 어제 늦던데")
        assert "어제 늦던데" in calls.planner_prompts[0]

    def test_combined_정제는_계획_입력에_직전_대화가_붙는다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(ps.REFINE_ENV, "combined")
        calls = Calls()
        _run(tmp_path, calls, "그거 왜 그래?", context="사용자: 어제 늦던데")
        assert "어제 늦던데" in calls.planner_prompts[0]

    def test_전부_꺼져_있으면_계획_입력은_요청_그대로다(self, tmp_path: Path) -> None:
        calls = Calls()
        _run(tmp_path, calls, "그거 왜 그래?", context="사용자: 어제 늦던데")
        assert calls.planner_prompts[0] == "그거 왜 그래?"


class TestCombinedRefineRules:
    """N3: combined refine added unstated targets and wrote unresolved guesses as settled (haiku 21/90)."""

    def test_combined_정제_지침은_원문에_없는_대상_추가와_unresolved_확정을_금한다(self) -> None:
        section = ps._COMBINED_REFINE_SECTION
        assert "원문에 없는 대상·지역·원천·기준·출력 형식·작업 범위를 더하지 않는다" in section
        assert "unresolved 에 적은 것을 refined 에 확정해 쓰지 않는다" in section
        assert "추정으로 채울 값은 assumed 에만 적는다" in section

    def test_combined_정제_지침은_근거_있는_추정을_assumed_로_채우게_한다(self) -> None:
        section = ps._COMBINED_REFINE_SECTION
        assert "basis 가 아니다" not in section
        assert "확률 높은 추정은 assumed 에 적고 ack 에 밝힌 뒤 진행한다" in section
