"""N1: 계획 호출의 입력 점검 필드(SCA_PLAN_INTAKE). 기록 전용."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_pipeline import build_pipeline, make_ctx, ok_response

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.engine.environment import ClaudeEnvironmentPolicy
from slack_cli_agent.guard import plan_start as ps
from slack_cli_agent.guard.pretool import plan_gate

# sha256 of the prompt and sorted-key schema JSON before the intake switch existed.
BASE_SHA = {
    False: ("6220e26a4f69b9cb3fff70a4ade7af8005812f08fa0c529dc65bd60f7ed0bae4",
            "14e0c330b64bb4f27ca2cf0283aed5b5bf9bca1f5d4bcfcfa9ce51c8e840bc75"),
    True: ("89c150d651a2bb611d4526ccc29ab9b84b66309ad82d915c98db4b4fc1642c9a",
           "ccf87ea31476569b4bfd5da67eed1d2af8df6ebdec3ad0599f4231e9d2a8392d"),
}
INTAKE: dict[str, Any] = {
    "intent": "서울 날씨 확인", "assumed": ["오늘 기준"], "missing": [], "ask": "",
    "expected_output": "한 줄 요약", "ack": "오늘 서울 기준으로 날씨를 확인해볼게요",
    "checks": [{"what": "기온 수치가 있다", "how": "웹 조회 결과"}],
}
BRANCH_PLAN: dict[str, Any] = {
    "type": "branch",
    "branches": [
        {"goal": "a", "source": "s", "depends_on": [], "delegate": "none", "returns": "수치"},
        {"goal": "b", "source": "s", "depends_on": [], "delegate": "none", "returns": "목록"},
    ],
}
SHORT_PLAN: dict[str, Any] = {"status": "ok", "type": "short", "branches": [], "finished_at": 9e9}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cli(plan: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    out = json.dumps({"is_error": False, "structured_output": plan, "total_cost_usd": 0.001})
    return subprocess.CompletedProcess([], 0, out, "")


def _run(tmp_path: Path, plan: dict[str, Any]) -> dict[str, Any]:
    transcript = str(tmp_path / "s.jsonl")
    ps.prompt_path(transcript).write_text("요청", encoding="utf-8")
    return ps.run(transcript, caller=lambda *_: _cli(plan))


class TestSwitchOffIsUnchanged:
    @pytest.mark.parametrize("delegation", [False, True])
    def test_꺼져_있으면_schema_와_지침이_변경_전_바이트와_같다(self, delegation: bool) -> None:
        prompt_sha, schema_sha = BASE_SHA[delegation]
        assert _sha(ps.planner_prompt(delegation)) == prompt_sha
        assert _sha(ps.planner_prompt(delegation, None, False)) == prompt_sha
        assert _sha(json.dumps(ps.schema(delegation), sort_keys=True)) == schema_sha
        assert _sha(json.dumps(ps.schema(delegation, None, False), sort_keys=True)) == schema_sha

    def test_상수는_그대로다(self) -> None:
        assert ps.planner_prompt(False) == ps.PLANNER_PROMPT
        assert ps.schema(False) == ps.SCHEMA

    def test_환경값_해석(self) -> None:
        assert not ps.intake_enabled({})
        assert not ps.intake_enabled({ps.INTAKE_ENV: "0"})
        assert not ps.intake_enabled({ps.INTAKE_ENV: " "})
        assert ps.intake_enabled({ps.INTAKE_ENV: "1"})


class TestSwitchOn:
    def test_schema_에_필드와_필수가_생긴다(self) -> None:
        result = ps.schema(False, None, True)
        for key in ("intent", "missing", "ask", "ack"):
            assert key in result["properties"] and key in result["required"]
        assert result["properties"]["checks"]["items"]["required"] == ["what", "how"]

    def test_지침은_요청_표식_앞에_붙고_해석_공개와_도구_확인을_적는다(self) -> None:
        text = ps.planner_prompt(False, None, True)
        assert text.endswith("\n[요청]\n")
        assert text.startswith(ps.planner_prompt(False)[: -len("\n[요청]\n")])
        assert "ack" in text and "추정한 해석" in text and "도구 결과로 확인되는 조건만" in text

    def test_workflow_와_함께면_workflow_뒤에_붙는다(self) -> None:
        text = ps.planner_prompt(False, {"deep": "기본"}, True)
        assert text.index("[workflow 후보]") < text.index("[입력 점검]") < text.rindex("[요청]")


class TestRun:
    def test_정상_필드는_계획_파일에_남는다(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ps.INTAKE_ENV, "1")
        record = _run(tmp_path, {**BRANCH_PLAN, **INTAKE})
        assert record["status"] == "ok"
        assert {key: record[key] for key in ps.INTAKE_FIELDS} == INTAKE
        assert "intake_dropped" not in record

    @pytest.mark.parametrize("bad", [
        {"intent": 3}, {"assumed": "x"}, {"missing": [1]}, {"ack": None},
        {"checks": [{"what": "a"}]}, {"checks": "x"},
    ])
    def test_틀린_필드만_버리고_status_와_계획은_유지한다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: dict[str, Any],
    ) -> None:
        monkeypatch.setenv(ps.INTAKE_ENV, "1")
        record = _run(tmp_path, {**BRANCH_PLAN, **INTAKE, **bad})
        key = next(iter(bad))
        assert record["status"] == "ok" and record["type"] == "branch" and len(record["branches"]) == 2
        assert key not in record and record["intake_dropped"] == [key]
        assert all(other in record for other in ps.INTAKE_FIELDS if other != key)

    def test_꺼져_있으면_모델이_낸_필드도_기록하지_않는다(self, tmp_path: Path) -> None:
        record = _run(tmp_path, {**BRANCH_PLAN, **INTAKE})
        assert record["status"] == "ok"
        assert not [key for key in ps.INTAKE_FIELDS if key in record]
        assert "intake_dropped" not in record


class TestInjectionUnchanged:
    def test_plan_gate_는_새_필드_값을_주입하지_않는다(self) -> None:
        plan = {**BRANCH_PLAN, **INTAKE, "intent": "의도값", "missing": ["빠진값"], "ask": "질문값", "ack": "응답값"}
        for text in (plan_gate.render(plan, intake=True), plan_gate.render_compact(plan)):
            for value in ("의도값", "빠진값", "질문값", "응답값"):
                assert value not in text


class TestPipeline:
    def _pipeline(self, tmp_path: Path, plan: Any, *, intake: bool = True, workflows: Any = None) -> tuple[Any, Any]:
        pipeline, deps = build_pipeline(responses=[ok_response()], tmp_path=tmp_path)
        pipeline._plan_intake = intake
        pipeline._plan_workflows = dict(workflows or {})
        pipeline._plan_result = lambda engine, session, workdir=None: plan
        return pipeline, deps

    def test_환경에_1_을_싣는다(self, tmp_path: Path) -> None:
        pipeline, _ = self._pipeline(tmp_path, None)
        pipeline._plan_gate_responses = 3
        assert pipeline._plan_gate_env("owner", None) == {"PLAN_GATE": "3", ps.INTAKE_ENV: "1"}

    def test_꺼져_있으면_환경에_싣지_않는다(self, tmp_path: Path) -> None:
        pipeline, _ = self._pipeline(tmp_path, None, intake=False)
        pipeline._plan_gate_responses = 3
        assert pipeline._plan_gate_env("owner", None) == {"PLAN_GATE": "3"}

    def test_workflow_후보_없이도_행에_남는다(self, tmp_path: Path) -> None:
        pipeline, deps = self._pipeline(tmp_path, {**SHORT_PLAN, **INTAKE})
        pipeline.handle(make_ctx())
        rec = deps["audit"].records[0]
        assert rec["plan_status"] == "ok"
        assert rec["plan_intent"] == INTAKE["intent"] and rec["plan_ack"] == INTAKE["ack"]
        assert rec["plan_assumed"] == ["오늘 기준"] and "plan_missing" not in rec and "plan_ask" not in rec
        assert rec["plan_checks_count"] == 1 and rec["plan_checks"] == INTAKE["checks"]
        assert "plan_workflow_id" not in rec

    def test_긴_문자열은_자른다(self, tmp_path: Path) -> None:
        plan = {**SHORT_PLAN, **INTAKE, "ack": "가" * 1000, "assumed": ["나" * 500] * 9}
        pipeline, deps = self._pipeline(tmp_path, plan)
        pipeline.handle(make_ctx())
        rec = deps["audit"].records[0]
        assert len(rec["plan_ack"]) == 300 and len(rec["plan_assumed"]) == 5 and len(rec["plan_assumed"][0]) == 100

    def test_꺼져_있고_후보도_없으면_필드가_없다(self, tmp_path: Path) -> None:
        pipeline, deps = self._pipeline(tmp_path, {**SHORT_PLAN, **INTAKE}, intake=False)
        pipeline.handle(make_ctx())
        assert not [key for key in deps["audit"].records[0] if key.startswith("plan_")]

    def test_workflow_와_함께여도_둘_다_남는다(self, tmp_path: Path) -> None:
        plan = {**SHORT_PLAN, **INTAKE, "workflow": "deep", "workflow_confidence": "low", "workflow_reason": "r"}
        pipeline, deps = self._pipeline(tmp_path, plan, workflows={"deep": "기본"})
        pipeline.handle(make_ctx())
        rec = deps["audit"].records[0]
        assert rec["plan_workflow_id"] == "deep" and rec["plan_intent"] == INTAKE["intent"]


class TestWiring:
    def test_설정_기본값은_꺼짐이다(self) -> None:
        assert RuntimeSettings().plan_intake is False

    def test_엔진_환경_허용목록에_있다(self) -> None:
        assert ps.INTAKE_ENV in ClaudeEnvironmentPolicy.ADDITIONAL_ALLOWED_VARS
