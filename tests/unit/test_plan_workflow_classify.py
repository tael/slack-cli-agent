"""계획 호출의 workflow 분류(기록 전용).

plan_start 가 후보 workflow 를 받으면 같은 호출에서 workflow, workflow_confidence,
workflow_reason 을 plan.json 에 남긴다. plan_gate 주입 문구와 모델·도구 동작은
그대로이고, 파이프라인은 결과를 request 행의 plan_* 필드로 남긴다.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_application import FakeSlackClient, write_profile
from test_pipeline import build_pipeline, make_ctx, ok_response

from slack_cli_agent.auth.principal import Principal, TrustLevel
from slack_cli_agent.core.application import Application
from slack_cli_agent.guard import plan_start as ps
from slack_cli_agent.guard.pretool import plan_gate
from slack_cli_agent.prompt.sections import CompositionContext

CANDIDATES = {
    "deep": "쓸 때: 기본값. 짧다고 가볍게 다루지 않는다",
    "web-lookup": "쓸 때: 웹에서 답이 나오는 조회. 예시 발화 \"오늘 서울 날씨\"",
}
BRANCH_PLAN: dict[str, Any] = {
    "type": "branch",
    "branches": [
        {"goal": "a", "source": "s", "depends_on": [], "delegate": "none", "returns": "수치"},
        {"goal": "b", "source": "s", "depends_on": [], "delegate": "none", "returns": "목록"},
    ],
}
CLASSIFIED = {**BRANCH_PLAN, "workflow": "web-lookup", "workflow_confidence": "high",
              "workflow_reason": "날씨 조회"}


def _cli(plan: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    out = json.dumps({"is_error": False, "structured_output": plan, "total_cost_usd": 0.001})
    return subprocess.CompletedProcess([], 0, out, "")


@pytest.fixture
def with_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ps.WORKFLOWS_ENV, json.dumps(CANDIDATES, ensure_ascii=False))


def _prepare(tmp_path: Path, request: str = "요청") -> str:
    transcript = str(tmp_path / "s.jsonl")
    ps.prompt_path(transcript).write_text(request, encoding="utf-8")
    return transcript


class TestSchemaAndPrompt:
    def test_후보가_없으면_schema_와_지침이_기존과_같다(self) -> None:
        assert ps.schema(False, None) == ps.schema(False)
        assert "workflow" not in ps.schema(False)["properties"]
        assert ps.planner_prompt(False, None) == ps.planner_prompt(False) == ps.PLANNER_PROMPT
        assert ps.planner_prompt(False, {}) == ps.PLANNER_PROMPT

    def test_후보가_있으면_enum_과_필수_필드가_생긴다(self) -> None:
        s = ps.schema(False, CANDIDATES)
        assert s["properties"]["workflow"]["enum"] == ["deep", "web-lookup"]
        assert s["properties"]["workflow_confidence"]["enum"] == ["low", "medium", "high"]
        assert {"workflow", "workflow_confidence", "workflow_reason"} <= set(s["required"])
        assert "type" in s["required"] and "branches" in s["required"]

    def test_지침에_후보와_설명이_들어가고_요청_표식은_맨_끝이다(self) -> None:
        text = ps.planner_prompt(False, CANDIDATES)
        assert "- web-lookup : 쓸 때: 웹에서 답이 나오는 조회" in text
        assert "확신이 낮으면 workflow 는 deep" in text
        assert text.endswith("[요청]\n")
        assert text.index("[workflow 후보]") < text.rindex("[요청]")

    def test_근거_길이_제한을_지침에_적는다(self) -> None:
        # 2026-11-16 표본 15건: 근거 문장이 길어 계획 호출이 평균 3.9초에서 8.7초가 됐다.
        assert "40자 이내" in ps.planner_prompt(False, CANDIDATES)

    def test_환경값_해석(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert ps.workflow_candidates({}) == {}
        assert ps.workflow_candidates({ps.WORKFLOWS_ENV: "깨짐"}) == {}
        assert ps.workflow_candidates({ps.WORKFLOWS_ENV: "[1]"}) == {}
        assert ps.workflow_candidates({ps.WORKFLOWS_ENV: json.dumps({"a": " 한  줄 ", "b": 3})}) == {"a": "한 줄"}


class TestRun:
    def test_분류가_계획_파일에_남는다(self, tmp_path: Path, with_candidates: None) -> None:
        transcript = _prepare(tmp_path)
        record = ps.run(transcript, caller=lambda *_: _cli(CLASSIFIED))
        saved = json.loads(ps.plan_path(transcript).read_text(encoding="utf-8"))
        assert record["status"] == saved["status"] == "ok"
        assert saved["workflow"] == "web-lookup"
        assert saved["workflow_confidence"] == "high"
        assert saved["workflow_reason"] == "날씨 조회"
        assert "workflow_error" not in saved

    def test_후보_밖_값은_버리고_사유를_남긴다(self, tmp_path: Path, with_candidates: None) -> None:
        transcript = _prepare(tmp_path)
        bad = {**CLASSIFIED, "workflow": "없는-것"}
        saved = ps.run(transcript, caller=lambda *_: _cli(bad))
        assert saved["status"] == "ok" and saved["workflow_error"] == "invalid"
        assert "workflow" not in saved and "workflow_confidence" not in saved

    def test_확신_값이_범위_밖이어도_버린다(self, tmp_path: Path, with_candidates: None) -> None:
        saved = ps.run(_prepare(tmp_path), caller=lambda *_: _cli({**CLASSIFIED, "workflow_confidence": "확실"}))
        assert saved["workflow_error"] == "invalid" and "workflow" not in saved

    def test_후보가_없으면_모델이_낸_분류도_기록하지_않는다(self, tmp_path: Path) -> None:
        saved = ps.run(_prepare(tmp_path), caller=lambda *_: _cli(CLASSIFIED))
        assert saved["status"] == "ok"
        assert not {"workflow", "workflow_confidence", "workflow_reason", "workflow_error"} & set(saved)

    def test_후보가_있어도_분류_누락은_분류_안_됨으로_남는다(self, tmp_path: Path, with_candidates: None) -> None:
        saved = ps.run(_prepare(tmp_path), caller=lambda *_: _cli(BRANCH_PLAN))
        assert saved["workflow_error"] == "invalid" and "workflow" not in saved

    def test_원인_분석_재요청은_요청_본문만으로_판정하고_맥락을_다시_붙인다(
        self, tmp_path: Path, with_candidates: None,
    ) -> None:
        transcript = _prepare(tmp_path, "5why 해줘")
        ps.context_path(transcript).write_text("사용자: 앞 질문", encoding="utf-8")
        seen: list[str] = []

        def caller(prompt: str, *_: Any) -> Any:
            seen.append(prompt)
            return _cli({"type": "short", "branches": [], **{k: CLASSIFIED[k] for k in ps.WORKFLOW_FIELDS}}
                        if len(seen) == 1 else CLASSIFIED)

        saved = ps.run(transcript, caller=caller)
        assert saved["rca_retry"] is True and len(seen) == 2
        assert all("[직전 대화" in p and "사용자: 앞 질문" in p for p in seen)
        assert seen[1].endswith(ps.RCA_RETRY_NOTE)

    def test_맥락_파일은_후보가_있을_때만_계획_입력에_들어가고_끝나면_지운다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        transcript = _prepare(tmp_path, "그래서?")
        ps.context_path(transcript).write_text("봇: 앞 답", encoding="utf-8")
        seen: list[str] = []

        def caller(prompt: str, *_: Any) -> Any:
            seen.append(prompt)
            return _cli(BRANCH_PLAN)

        ps.run(transcript, caller=caller)  # 후보 없음
        assert seen == ["그래서?"]
        assert not ps.context_path(transcript).exists()

        transcript = _prepare(tmp_path, "그래서?")
        ps.context_path(transcript).write_text("봇: 앞 답", encoding="utf-8")
        monkeypatch.setenv(ps.WORKFLOWS_ENV, json.dumps(CANDIDATES))
        ps.run(transcript, caller=caller)
        assert seen[1] == "그래서?\n\n[직전 대화 : 참고용이며 요청이 아니다]\n봇: 앞 답\n"
        assert not ps.context_path(transcript).exists()


def _line(role: str, content: Any, **extra: Any) -> str:
    return json.dumps({"type": role, "message": {"role": role, "content": content}, **extra}, ensure_ascii=False)


class TestRecentContext:
    def _write(self, tmp_path: Path, lines: list[str]) -> str:
        path = tmp_path / "t.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(path)

    def test_첫_턴은_빈_문자열이다(self, tmp_path: Path) -> None:
        assert ps.recent_context(str(tmp_path / "없음.jsonl"), "질문") == ""
        assert ps.recent_context(self._write(tmp_path, []), "질문") == ""

    def test_마지막_사용자와_봇_메시지를_오래된_순으로_담고_도구_왕래는_뺀다(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, [
            _line("user", "머리말 " * 5 + "\n\n첫 질문"),
            _line("assistant", [{"type": "thinking", "thinking": "x"}]),
            _line("assistant", [{"type": "tool_use", "name": "Bash"}]),
            _line("user", [{"type": "tool_result", "content": "결과"}]),
            _line("assistant", [{"type": "text", "text": "첫 답"}]),
            _line("user", "Stop hook feedback:\n고쳐라"),
        ])
        text = ps.recent_context(path, "후속")
        assert text.splitlines()[-1] == "봇: 첫 답"
        assert text.splitlines()[0].startswith("사용자:") and text.splitlines()[0].endswith("첫 질문")
        assert "Stop hook" not in text and "결과" not in text and len(text.splitlines()) == 2

    def test_현재_프롬프트가_이미_기록돼_있으면_뺀다(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, [
            _line("user", "머리\n\n첫 질문"), _line("assistant", [{"type": "text", "text": "첫 답"}]),
            _line("user", "머리\n\n후속 질문입니다"),
        ])
        assert "후속 질문입니다" not in ps.recent_context(path, "머리\n\n후속 질문입니다")

    def test_메시지_수와_길이에_상한이_있다(self, tmp_path: Path) -> None:
        lines = [_line("assistant", [{"type": "text", "text": f"{i}번 " + "가" * 900}]) for i in range(10)]
        text = ps.recent_context(self._write(tmp_path, lines), "q")
        assert len(text.splitlines()) == ps.CONTEXT_MESSAGES
        assert len(text) <= ps.CONTEXT_TOTAL_CHARS
        assert "9번" in text or text.endswith("…")
        assert all(len(row) <= ps.CONTEXT_MESSAGE_CHARS + 10 for row in text.splitlines())

    def test_서브에이전트_줄과_깨진_줄은_건너뛴다(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, [
            "깨짐", _line("assistant", [{"type": "text", "text": "서브"}], isSidechain=True),
            _line("assistant", [{"type": "text", "text": "본 답"}]),
        ])
        assert ps.recent_context(path, "q") == "봇: 본 답"


class TestStart:
    def _data(self, tmp_path: Path) -> dict[str, Any]:
        return {"transcript_path": str(tmp_path / "s.jsonl"), "prompt": "후속 질문"}

    def _run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, candidates: bool) -> str:
        monkeypatch.setenv("PLAN_GATE", "3")
        monkeypatch.delenv(ps.PLAN_NESTED_ENV, raising=False)
        if candidates:
            monkeypatch.setenv(ps.WORKFLOWS_ENV, json.dumps(CANDIDATES))
        (tmp_path / "s.jsonl").write_text(
            _line("assistant", [{"type": "text", "text": "앞 답"}]) + "\n", encoding="utf-8")
        assert ps.start(self._data(tmp_path), spawn=lambda *a, **k: None) == "started"
        return str(tmp_path / "s.jsonl")

    def test_후보가_있으면_맥락_파일을_쓴다(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        transcript = self._run(tmp_path, monkeypatch, candidates=True)
        assert ps.context_path(transcript).read_text(encoding="utf-8") == "봇: 앞 답"

    def test_후보가_없으면_맥락을_읽지_않는다(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        transcript = self._run(tmp_path, monkeypatch, candidates=False)
        assert not ps.context_path(transcript).exists()

    def test_앞_요청의_맥락_파일을_지운다(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        transcript = str(tmp_path / "s.jsonl")
        ps.context_path(transcript).write_text("낡음", encoding="utf-8")
        self._run(tmp_path, monkeypatch, candidates=False)
        assert not ps.context_path(transcript).exists()


class TestInjectionUnchanged:
    def test_분류_필드는_주입_문구에_영향이_없다(self) -> None:
        assert plan_gate.render(CLASSIFIED) == plan_gate.render(BRANCH_PLAN)
        assert "workflow" not in plan_gate.render(CLASSIFIED)

    def test_분류만_있고_단답이면_주입하지_않는다(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PLAN_GATE", "3")
        transcript = str(tmp_path / "s.jsonl")
        short = {"status": "ok", "type": "short", "branches": [], "finished_at": 9e9,
                 "workflow": "deep", "workflow_confidence": "low", "workflow_reason": "r"}
        plan_gate.plan_path(transcript)
        Path(plan_gate.plan_path(transcript)).write_text(json.dumps(short), encoding="utf-8")
        assert plan_gate.decide({"transcript_path": transcript}, now=9e9)[0] == "short"


# --- 파이프라인 ---------------------------------------------------------------

def _plan(**kw: Any) -> dict[str, Any]:
    return {"status": "ok", "type": "short", "branches": [], "finished_at": 9e9, **kw}


def _pipeline(tmp_path: Path, plan: Any, *, candidates: Any = CANDIDATES) -> tuple[Any, Any, list[tuple[str, str]]]:
    asked: list[tuple[str, str]] = []

    def reader(engine: str, session: str, workdir: Any = None) -> Any:
        asked.append((engine, session))
        if isinstance(plan, Exception):
            raise plan
        return plan

    pipeline, deps = build_pipeline(responses=[ok_response()], tmp_path=tmp_path)
    pipeline._plan_workflows = dict(candidates or {})
    pipeline._plan_result = reader
    return pipeline, deps, asked


class TestAuditFields:
    def test_분류가_있으면_id_와_확신과_근거가_남는다(self, tmp_path: Path) -> None:
        pipeline, deps, asked = _pipeline(tmp_path, _plan(
            workflow="web-lookup", workflow_confidence="high", workflow_reason="날씨"))
        pipeline.handle(make_ctx())
        rec = deps["audit"].records[0]
        assert rec["plan_workflow_id"] == "web-lookup"
        assert rec["plan_workflow_confidence"] == "high"
        assert rec["plan_workflow_reason"] == "날씨"
        assert rec["plan_status"] == "ok"
        assert asked and asked[0][1]

    @pytest.mark.parametrize("status", ["pending", "timeout", "error:cli", "bad_output"])
    def test_계획이_끝나지_않았거나_실패면_status_만_남는다(self, tmp_path: Path, status: str) -> None:
        pipeline, deps, _ = _pipeline(tmp_path, {"status": status, "started_at": 9e9})
        pipeline.handle(make_ctx())
        rec = deps["audit"].records[0]
        assert rec["plan_status"] == status
        assert "plan_workflow_id" not in rec

    def test_계획은_성공했으나_분류가_없으면_ok_no_workflow(self, tmp_path: Path) -> None:
        pipeline, deps, _ = _pipeline(tmp_path, _plan(workflow_error="invalid"))
        pipeline.handle(make_ctx())
        assert deps["audit"].records[0]["plan_status"] == "ok_no_workflow"

    def test_폴백_엔진이면_이번_턴에_실행될_엔진_이름으로_계획을_읽는다(self, tmp_path: Path) -> None:
        pipeline, _, asked = _pipeline(tmp_path, _plan(workflow="web-lookup",
                                                          workflow_confidence="high", workflow_reason="r"))

        class _Fallback(type(pipeline._engine)):  # type: ignore[misc]
            name = "fallback"

            def serving_name(self) -> str:
                return "claude"

        pipeline._engine = _Fallback()
        pipeline.handle(make_ctx())
        assert asked and {a[0] for a in asked} == {"claude"}

    def test_계획_파일이_없으면_no_plan_file(self, tmp_path: Path) -> None:
        pipeline, deps, _ = _pipeline(tmp_path, None)
        pipeline.handle(make_ctx())
        assert deps["audit"].records[0]["plan_status"] == "no_plan_file"

    def test_이_요청보다_먼저_끝난_계획은_stale(self, tmp_path: Path) -> None:
        pipeline, deps, _ = _pipeline(tmp_path, _plan(finished_at=1.0, workflow="deep",
                                                      workflow_confidence="low", workflow_reason="r"))
        pipeline.handle(make_ctx())
        rec = deps["audit"].records[0]
        assert rec["plan_status"] == "stale" and "plan_workflow_id" not in rec

    def test_읽기_예외는_삼키고_read_error(self, tmp_path: Path) -> None:
        pipeline, deps, _ = _pipeline(tmp_path, RuntimeError("고장"))
        outcome = pipeline.handle(make_ctx())
        assert outcome.ok is True
        assert deps["audit"].records[0]["plan_status"] == "read_error"

    def test_후보가_없으면_필드를_쓰지_않는다(self, tmp_path: Path) -> None:
        pipeline, deps, asked = _pipeline(tmp_path, _plan(), candidates={})
        pipeline.handle(make_ctx())
        assert not [k for k in deps["audit"].records[0] if k.startswith("plan_")]
        assert asked == []

    def test_읽는_함수가_없으면_필드를_쓰지_않는다(self, tmp_path: Path) -> None:
        pipeline, deps, _ = _pipeline(tmp_path, _plan())
        pipeline._plan_result = None
        pipeline.handle(make_ctx())
        assert not [k for k in deps["audit"].records[0] if k.startswith("plan_")]


class TestPipelineEnv:
    def test_후보가_있으면_환경에_JSON_으로_싣는다(self, tmp_path: Path) -> None:
        pipeline, _, _ = _pipeline(tmp_path, None)
        pipeline._plan_gate_responses = 3
        env = pipeline._plan_gate_env("owner", None)
        assert json.loads(env[ps.WORKFLOWS_ENV]) == CANDIDATES
        assert env["PLAN_GATE"] == "3"

    def test_계획_흐름이_꺼진_턴에는_싣지_않는다(self, tmp_path: Path) -> None:
        pipeline, _, _ = _pipeline(tmp_path, None)
        pipeline._plan_gate_responses = 0
        assert pipeline._plan_gate_env("owner", None) == {}

    def test_후보가_없으면_PLAN_GATE_만_싣는다(self, tmp_path: Path) -> None:
        pipeline, _, _ = _pipeline(tmp_path, None, candidates={})
        pipeline._plan_gate_responses = 3
        assert pipeline._plan_gate_env("owner", None) == {"PLAN_GATE": "3"}

    def test_엔진_환경_허용목록에_있다(self) -> None:
        from slack_cli_agent.engine.environment import ClaudeEnvironmentPolicy

        assert ps.WORKFLOWS_ENV in ClaudeEnvironmentPolicy.ADDITIONAL_ALLOWED_VARS


class TestRegistryCandidates:
    def test_꺼진_레지스트리는_빈_후보다(self) -> None:
        from slack_cli_agent.registry.workflows import WorkflowRegistry, classification_candidates

        assert classification_candidates(WorkflowRegistry(False, "x", {}, ())) == {}


# --- 지침 바이트 불변 ---------------------------------------------------------

#: 이 변경 이전(HEAD fc9887d3) 실물 조립기로 낸 운영 조립 지침의 sha256.
#: 지침을 고치는 변경이면 이 값을 의도해서 바꾼다.
#: 2026-11-16 owner-* : 소유자 턴에 SelfConfigSection(자기 설정 위치) 추가.
#: 2026-11-20 owner-* : 소유자 턴에 MemoryPathSection(메모리 쓰기 경로) 추가.
#: 2026-11-20 owner-work : flow_note 에 완료 조건 문구 추가.
#: 2026-11-27 전부 : RECIPIENT_RULE 에 침묵 사유 단어 안내 추가.
GOLDEN = {
    "owner-chat": "8feea194da19bd705c659b2e3e6eb67d665d88c0496981f4132c13fb9298bda0",
    "owner-work": "5fa3e2672d3c9cc8cb4cf5f599ab54f9c75212f1ad534a8062b18451ca3da51a",
    "read-work": "ef86116284ac7eec0a03cf88dd25b5ff52086cfaa864c71101a818066a213b3b",
}


class TestInstructionBytes:
    OWNER = Principal(user_id="U_OWNER", channel="C1", trust=TrustLevel.OWNER, is_direct_message=False)
    READER = Principal(user_id="U_X", channel="C1", trust=TrustLevel.GENERAL, is_direct_message=False)

    def _digest(self, tmp_path: Path, principal: Principal, mode: str, tier: str) -> str:
        profile = write_profile(tmp_path)
        app = Application(profile, FakeSlackClient())
        try:
            ctx = CompositionContext(
                principal=principal, channel_mode=mode, channel="C1", chat_level="normal",
                turn_tier=tier, request_header=True, watch_out_dir="/fixed/watch-out",
                prompt="안녕", thread_ts="1.1", watch_run_id="run-1", delegation=True,
            )
            text = app._composer().compose(ctx)
        finally:
            app.close()
        text = text.replace(str(tmp_path), "<TMP>")
        assert "workflow_confidence" not in text and "SCA_PLAN_WORKFLOWS" not in text
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @pytest.mark.parametrize(("name", "who", "mode", "tier"), [
        ("owner-chat", "OWNER", "chat", "owner"),
        ("owner-work", "OWNER", "work", "owner"),
        ("read-work", "READER", "work", "read"),
    ])
    def test_운영_조립_지침이_변경_전과_같다(self, tmp_path: Path, name: str, who: str, mode: str, tier: str) -> None:
        digest = self._digest(tmp_path, getattr(self, who), mode, tier)
        assert digest == GOLDEN[name], f"{name}: {digest}"
