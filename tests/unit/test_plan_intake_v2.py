"""Plan intake schema v2: intake_view, planner schema, injection (sca-blrc.11)."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_pipeline import build_pipeline, make_ctx, ok_response
from test_plan_intake import BRANCH_PLAN, INTAKE, SHORT_PLAN

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.pipeline import RequestPipeline
from slack_cli_agent.engine.environment import ClaudeEnvironmentPolicy
from slack_cli_agent.guard import finish_review as fr
from slack_cli_agent.guard import plan_start as ps
from slack_cli_agent.guard.pretool import plan_gate as pg


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


V2: dict[str, Any] = {
    "intent": "어제 신지 응답 중 가장 오래 걸린 요청 찾기",
    "inputs": [
        {"name": "날짜", "value": "2026-11-18", "kind": "derived", "reason": "현재 시각 기준 어제"},
        {"name": "audit 경로", "value": "~/.shinji/audit.jsonl", "kind": "lookup", "reason": "파일로 찾는다"},
        {"name": "대상 봇", "value": "신지", "kind": "guess", "reason": "요청에 신지가 있다"},
        {"name": "요청 원문", "value": "어제 신지 응답", "kind": "given", "reason": ""},
    ],
    "ask": "",
    "output": {"form": "목록", "items": ["시각", "채널", "요청 요지", "소요 초"]},
    "ack": "신지 기준으로 어제 응답을 찾아볼게요",
}
V2_BRANCH: dict[str, Any] = {**BRANCH_PLAN, **V2}
V2_SHORT: dict[str, Any] = {**SHORT_PLAN, **V2}
ODD_V1: dict[str, Any] = {
    **INTAKE, "assumed": [1, "a "], "missing": ["m"],
    "checks": [{"what": " x ", "how": "y"}, "z", {"what": "", "how": ""}, {"what": 3}],
}


class TestV1Unchanged:
    def test_v1_주입문은_바이트가_같다(self) -> None:
        assert _sha(pg.render({**BRANCH_PLAN, **INTAKE}, intake=True)) == (
            "dbb0bd197b3be522e4a07737ad40cb4f53fc29728e02526395042d4aa0f11577")
        assert _sha(pg.render_compact({**SHORT_PLAN, **INTAKE})) == (
            "9cc2dbf9da7490dd1424b3b1c4a8dab78e9ae279d7d5be2eb4382bd7ea11263d")
        assert _sha(pg.render({**BRANCH_PLAN, **ODD_V1}, intake=True)) == (
            "d3b046c82e7670a1ef5a0a1a4c5e1e379df028f38be3939d3485aeb6a6da1d91")

    def test_v1_audit_필드는_바이트가_같다(self) -> None:
        def dump(plan: dict[str, Any]) -> str:
            return json.dumps(RequestPipeline._plan_intake_fields(plan), sort_keys=True, ensure_ascii=False)

        assert _sha(dump(ODD_V1)) == "ea81071e90875b729bc5ea680e1bfa329c11aecf85b53c7b0e6f771ebc9da959"
        assert _sha(dump({**SHORT_PLAN, **INTAKE})) == (
            "8d24d881983f9b0298c8fb0c509e7b240cf43a5de2a9cc7205a890abbf150840")

    def test_v1_점검_기준(self) -> None:
        assert fr.plan_checks({"status": "ok", **INTAKE}) == [{"what": "기온 수치가 있다", "how": "웹 조회 결과"}]


class TestIntakeView:
    def test_v2_계획은_입력과_출력을_돌려준다(self) -> None:
        view = pg.intake_view(V2)
        assert view.schema == 2
        assert [i["kind"] for i in view.inputs] == ["derived", "lookup", "guess", "given"]
        assert view.output_form == "목록" and view.output_items == ["시각", "채널", "요청 요지", "소요 초"]
        assert view.assumed == ["대상 봇: 신지"] and view.missing == []
        assert view.checks is None

    def test_v1_계획은_스키마_1(self) -> None:
        view = pg.intake_view(INTAKE)
        assert view.schema == 1 and view.inputs == [] and view.output_form == ""
        assert view.expected == "한 줄 요약" and view.assumed == ["오늘 기준"]

    def test_틀린_항목은_버린다(self) -> None:
        plan = {
            "inputs": [
                {"name": "a", "value": "1", "kind": "ask", "reason": "r"},
                {"name": "b", "value": "1", "kind": "bogus", "reason": ""},
                {"name": "", "value": "1", "kind": "given", "reason": ""},
                "x",
                {"name": "c", "value": 3, "kind": "given", "reason": ""},
            ] + [{"name": f"n{i}", "value": "v", "kind": "given", "reason": ""} for i in range(8)],
            "output": {"form": "표", "items": ["a", 3, " ", "b"] + ["c"] * 8},
        }
        view = pg.intake_view(plan)
        assert [i["name"] for i in view.inputs][:2] == ["a", "n0"] and len(view.inputs) == 6
        assert view.output_items == ["a", "b"] + ["c"] * 4
        assert view.missing == ["a"]

    def test_출력_형태가_enum_밖이면_출력을_뺀다(self) -> None:
        view = pg.intake_view({"inputs": [], "output": {"form": "시", "items": ["a"]}})
        assert view.output_form == "" and view.output_items == []


class TestInjection:
    def test_v2_전체_주입(self) -> None:
        text = pg.render(V2_BRANCH, intake=True)
        assert "날짜: 2026-11-18 (derived)" in text and "audit 경로: ~/.shinji/audit.jsonl (lookup)" in text
        assert "대상 봇: 신지 (guess)" in text
        assert "기대 결과 : 목록 / 시각, 채널, 요청 요지, 소요 초" in text
        assert "lookup 입력은 도구로 찾아 채우고 '미확인' 으로 남기지 않는다" in text
        assert pg.INJECT_GUESS.removeprefix("- ") in text
        assert "도구 결과가 다르면 바꾸고 바꾼 점을 답에 한 문장으로 밝힌다" in text
        assert "답은 기대 결과의 형태와 항목대로 쓰고 그 밖의 내용은 덧붙이지 않는다" in text
        assert pg.INJECT_CHECKS not in text and "답변은 이 기준으로 점검된다" not in text
        assert "확인 기준" not in text and "가정 :" not in text

    def test_v2_짧은_계획도_주입한다(self) -> None:
        assert pg.has_compact_intake(V2_SHORT)
        text = pg.render_compact(V2_SHORT)
        assert text.startswith(pg.INJECT_HEAD_COMPACT) and "기대 결과 : 목록" in text
        assert "미확인" not in text.replace("'미확인' 으로 남기지 않는다", "")

    def test_입출력이_없는_v2_짧은_계획은_주입하지_않는다(self) -> None:
        plan = {**SHORT_PLAN, "inputs": [], "output": {"form": "", "items": []}, "ack": "a"}
        assert not pg.has_compact_intake(plan)

    def test_lookup_과_guess_가_없으면_해당_규칙은_빠진다(self) -> None:
        plan = {**V2_BRANCH, "inputs": [V2["inputs"][0]]}
        text = pg.render(plan, intake=True)
        assert "lookup 입력은" not in text and "guess 값은" not in text
        assert "답은 기대 결과의 형태와 항목대로" in text

    def test_decide_는_v2_짧은_계획에_compact_표지를_남긴다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        transcript = str(tmp_path / "t.jsonl")
        Path(transcript + ".plan.json").write_text(
            json.dumps({**V2_SHORT, "finished_at": 9e9}), encoding="utf-8")
        monkeypatch.setenv(pg.PLAN_GATE_ENV, "3")
        monkeypatch.setenv(pg.INTAKE_INJECT_ENV, "1")
        verdict, context = pg.decide({"transcript_path": transcript}, now=9e9)
        assert verdict == "inject" and "날짜: 2026-11-18 (derived)" in context
        assert pg.injected_path(transcript).read_text() == pg.KIND_COMPACT

    def test_v1_짧은_계획은_v2_가_아니라서_그대로(self) -> None:
        assert pg.has_compact_intake({**SHORT_PLAN, **INTAKE})


class TestPlanStartV2:
    def test_schema_v2(self) -> None:
        result = ps.schema(False, None, True, "", 2)
        props = result["properties"]
        for key in ("intent", "inputs", "ask", "output", "ack"):
            assert key in props and key in result["required"]
        for gone in ("assumed", "missing", "expected_output", "checks"):
            assert gone not in props and gone not in result["required"]
        inputs = props["inputs"]
        assert inputs["maxItems"] == 6
        assert inputs["items"]["properties"]["kind"]["enum"] == ["given", "derived", "lookup", "guess", "ask"]
        assert inputs["items"]["required"] == ["name", "value", "kind", "reason"]
        out = props["output"]["properties"]
        assert out["form"]["enum"] == ["한두줄", "문단", "목록", "표", "긴글"]
        assert out["items"]["maxItems"] == 6

    def test_상수는_plan_gate_와_같다(self) -> None:
        assert ps.INPUT_KINDS == pg.INPUT_KINDS and ps.OUTPUT_FORMS == pg.OUTPUT_FORMS
        assert ps.INPUTS_MAX == pg.INPUTS_MAX and ps.OUTPUT_ITEMS_MAX == pg.OUTPUT_ITEMS_MAX

    def test_지침_v2(self) -> None:
        text = ps.planner_prompt(False, None, True, None, "", 2)
        assert text.endswith("\n[요청]\n") and "[입력 점검]" in text
        for word in ("lookup", "guess", "kind", "form", "ack"):
            assert word in text
        assert "assumed" not in text and "missing" not in text and "checks" not in text
        assert "질문은 넣지 않는다" in text

    def test_스키마_1_과_꺼짐은_바이트가_같다(self) -> None:
        assert ps.planner_prompt(False, None, True, None, "", 1) == ps.planner_prompt(False, None, True)
        assert ps.schema(False, None, True, "", 1) == ps.schema(False, None, True)
        assert ps.planner_prompt(False, None, False, None, "", 2) == ps.planner_prompt(False)
        assert ps.schema(False, None, False, "", 2) == ps.schema(False)

    def test_환경값_해석(self) -> None:
        assert ps.intake_schema({}) == 1
        assert ps.intake_schema({ps.INTAKE_SCHEMA_ENV: "2"}) == 2
        assert ps.intake_schema({ps.INTAKE_SCHEMA_ENV: "x"}) == 1

    def test_check_intake_v2_정상은_그대로(self) -> None:
        plan = json.loads(json.dumps({**V2, "ask": ""}))
        assert ps.check_intake(plan, True, 2) == []
        assert plan == V2

    def test_ask_종류가_없는데_ask_가_있으면_비운다(self) -> None:
        plan = json.loads(json.dumps({**V2, "ask": "어느 봇인가요?"}))
        ps.check_intake(plan, True, 2)
        assert plan["ask"] == ""

    def test_ask_종류가_있으면_ask_를_둔다(self) -> None:
        inputs = [*V2["inputs"], {"name": "수신자", "value": "", "kind": "ask", "reason": "정할 수 없다"}]
        plan = json.loads(json.dumps({**V2, "inputs": inputs, "ask": "누구에게 보낼까요?"}))
        ps.check_intake(plan, True, 2)
        assert plan["ask"] == "누구에게 보낼까요?"

    def test_틀린_항목을_버리고_필드_전체가_틀리면_필드를_버린다(self) -> None:
        plan = json.loads(json.dumps({**V2, "inputs": [*V2["inputs"], {"name": "x", "kind": "zzz"}, "q"]}))
        dropped = ps.check_intake(plan, True, 2)
        assert plan["inputs"] == V2["inputs"] and dropped == []
        plan = {**V2, "output": "x", "ack": 3}
        dropped = ps.check_intake(plan, True, 2)
        assert "output" not in plan and "ack" not in plan and sorted(dropped) == ["ack", "output"]

    def test_출력_항목_상한과_형태(self) -> None:
        plan = {**V2, "output": {"form": "목록", "items": ["a"] * 9 + [3]}}
        ps.check_intake(plan, True, 2)
        assert plan["output"] == {"form": "목록", "items": ["a"] * 6}
        plan = {**V2, "output": {"form": "시", "items": ["a"]}}
        assert ps.check_intake(plan, True, 2) == ["output"]

    def test_v1_필드는_v2_에서_버린다(self) -> None:
        plan = {**V2, "assumed": ["a"], "missing": [], "expected_output": "x", "checks": []}
        ps.check_intake(plan, True, 2)
        assert not [k for k in ("assumed", "missing", "expected_output", "checks") if k in plan]

    def test_run_은_보정을_기록한다(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ps.INTAKE_ENV, "1")
        monkeypatch.setenv(ps.INTAKE_SCHEMA_ENV, "2")
        transcript = str(tmp_path / "s.jsonl")
        ps.prompt_path(transcript).write_text("요청", encoding="utf-8")
        plan = {**BRANCH_PLAN, **V2, "ask": "되묻기"}
        out = json.dumps({"is_error": False, "structured_output": plan, "total_cost_usd": 0.001})
        record = ps.run(transcript, caller=lambda *_: subprocess.CompletedProcess([], 0, out, ""))
        assert record["status"] == "ok" and record["ask"] == "" and record["intake_fixed"] == ["ask"]
        assert record["inputs"] == V2["inputs"]


class TestPipelineV2:
    def _pipeline(self, tmp_path: Path, plan: Any, schema: int = 2) -> tuple[Any, Any]:
        pipeline, deps = build_pipeline(responses=[ok_response()], tmp_path=tmp_path)
        pipeline._plan_intake = True
        pipeline._plan_intake_schema = schema
        pipeline._plan_workflows = {}
        pipeline._plan_result = lambda engine, session, workdir=None: plan
        pipeline._plan_gate_responses = 3
        return pipeline, deps

    def test_환경은_스키마_2_일_때만_싣는다(self, tmp_path: Path) -> None:
        for schema, expected in [(2, "2"), (1, None)]:
            pipeline, _ = self._pipeline(tmp_path, None, schema)
            assert pipeline._plan_gate_env("owner", None).get(ps.INTAKE_SCHEMA_ENV) == expected
        pipeline, _ = self._pipeline(tmp_path, None, 2)
        pipeline._plan_intake = False
        assert ps.INTAKE_SCHEMA_ENV not in pipeline._plan_gate_env("owner", None)

    def test_audit_키는_v2_에서도_남는다(self, tmp_path: Path) -> None:
        inputs = [*V2["inputs"], {"name": "수신자", "value": "", "kind": "ask", "reason": "x"}]
        pipeline, deps = self._pipeline(tmp_path, {**SHORT_PLAN, **V2, "inputs": inputs, "ask": "누구?"})
        pipeline.handle(make_ctx())
        rec = deps["audit"].records[0]
        assert rec["plan_assumed"] == ["대상 봇: 신지"] and rec["plan_missing"] == ["수신자"]
        assert rec["plan_intent"] == V2["intent"] and rec["plan_ack"] == V2["ack"] and rec["plan_ask"] == "누구?"
        assert "plan_checks_count" not in rec

    def test_설정과_허용목록(self) -> None:
        assert RuntimeSettings().plan_intake_schema == 1
        assert ps.INTAKE_SCHEMA_ENV in ClaudeEnvironmentPolicy.ADDITIONAL_ALLOWED_VARS


class TestFinishReviewView:
    def test_v2_계획은_점검_기준이_없다(self) -> None:
        assert fr.plan_checks({"status": "ok", **V2}) == []


def test_v2_prompt_keeps_write_targets_out_of_guess() -> None:
    # N3 schema2: haiku filled write targets (channel, date) as guess and left ask empty (s25 s28 s29).
    prompt = ps.planner_prompt(intake=True, intake_version=2)
    assert "쓰기·전송·삭제의 대상은 guess 로 채우지 않는다" in prompt


def test_v2_prompt_lists_interpretation_inputs_and_identifying_items() -> None:
    prompt = ps.planner_prompt(intake=True, intake_version=2)
    assert "고르는 기준·포함 범위처럼 해석이 갈리는 값도 입력이다" in prompt
    assert "items 는 요청이 직접 물은 것만" in prompt
    assert "조건부 항목과 추정 공개는 넣지 않는다" in prompt


def test_v2_one_or_two_line_output_keeps_two_items() -> None:
    # 11-19 replay: 한두줄 plans listed 3-4 items, so the output check found "missing" items the request never asked.
    plan = {"output": {"form": "한두줄", "items": ["기온", "날씨", "습도", "풍속"]}}
    ps.check_intake_v2(plan)
    assert plan["output"]["items"] == ["기온", "날씨"]
    table = {"output": {"form": "표", "items": ["a", "b", "c"]}}
    ps.check_intake_v2(table)
    assert table["output"]["items"] == ["a", "b", "c"]


def test_demote_unsupported_guess_makes_lookup_and_neutral_ack():
    from slack_cli_agent.guard.plan_start import demote_unsupported_guess

    plan = {"ack": "Neon 서버리스 Postgres 로 보고 찾을게요", "inputs": [
        {"name": "대상", "value": "Neon serverless Postgres", "kind": "guess", "reason": ""},
        {"name": "기간", "value": "오늘", "kind": "guess", "reason": ""}]}
    assert demote_unsupported_guess(plan, "구글 NEON 에 대해 알려줘") == ["대상"]
    plan2 = {"ack": "x", "inputs": [{"name": "대상", "value": "Postgres", "kind": "guess", "reason": ""}]}
    assert demote_unsupported_guess(plan2, "구글 NEON 알려줘") == ["대상"]
    assert plan2["inputs"][0]["kind"] == "lookup" and "먼저 확인" in plan2["ack"]
