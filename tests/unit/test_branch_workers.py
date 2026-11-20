"""단계 C 시험 : core/branch_workers 와 파이프라인 연결."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from slack_cli_agent.core import branch_workers as bw
from slack_cli_agent.engine.base import EngineRequest, EngineResponse

_PLAN = {
    "type": "branch",
    "branches": [
        {"goal": "어제 주문 수", "source": "DATAMART.ORDERS", "depends_on": [], "delegate": "analyst",
         "returns": "수치"},
        {"goal": "상점 목록", "source": "stores.csv", "depends_on": [], "delegate": "lookup",
         "returns": "목록"},
        {"goal": "둘의 비율", "source": "1, 2 결과", "depends_on": [1, 2], "delegate": "none",
         "returns": "수치"},
    ],
}


def _request() -> EngineRequest:
    return EngineRequest(prompt="본 요청", system_prompt="지침", session_id="s0", resume=True,
                         model="opus", effort="high", workdir=Path("/tmp"), request_id="r1",
                         engine_env={"PLAN_GATE": "3", "TURN_ID": "C1:1"})


def _ok(body: str) -> EngineResponse:
    return EngineResponse(ok=True, body=body, session_id="w", model_actual=None, elapsed=1.0,
                          turns=1, usage=None)


class TestBranches:
    def test_의존_없는_갈래만_둘_이상일_때_고른다(self) -> None:
        assert [b["goal"] for b in bw.independent_branches(_PLAN)] == ["어제 주문 수", "상점 목록"]

    def test_위임_표시가_없는_갈래는_고르지_않는다(self) -> None:
        plan = {"type": "branch", "branches": [{**b, "delegate": "none"} for b in _PLAN["branches"]]}
        assert bw.independent_branches(plan) == []

    @pytest.mark.parametrize("plan", [None, {"type": "short", "branches": []},
                                      {"type": "explore", "branches": _PLAN["branches"]},
                                      {"type": "branch", "branches": _PLAN["branches"][:1]}])
    def test_그_밖에는_고르지_않는다(self, plan: Any) -> None:
        assert bw.independent_branches(plan) == []


class TestWorkerRequest:
    def test_세션_없이_한_갈래만_맡기고_계획과_검수_모델을_끈다(self) -> None:
        req = bw.worker_request(_request(), _PLAN["branches"][0], 1, "어제 주문과 상점 목록", "sonnet")
        assert req.session_id is None and req.resume is False
        assert req.model == "sonnet" and req.progress_log is None
        assert "PLAN_GATE" not in req.engine_env
        assert req.engine_env["SCA_FINISH_VERIFY"] == "0"
        assert req.engine_env["TURN_ID"] == "C1:1"
        assert req.request_id == "r1:w1"
        assert "목표 : 어제 주문 수" in req.prompt and "조회만 한다" in req.prompt
        assert req.system_prompt == "지침"

    def test_모델을_비우면_본_턴_모델을_쓴다(self) -> None:
        assert bw.worker_request(_request(), _PLAN["branches"][0], 1, "x", None).model == "opus"


class TestPrepare:
    def _planner(self, status: str = "ok", plan: Any = _PLAN) -> Any:
        return lambda text, model: (status, plan, 5.0)

    def test_작업자를_돌려_결과를_본_요청에_붙인다(self) -> None:
        seen: list[EngineRequest] = []

        def invoke(req: EngineRequest) -> EngineResponse:
            seen.append(req)
            return _ok(f"결론 : {req.request_id}")

        req, run = bw.prepare(invoke, _request(), "요청", planner=self._planner())
        assert len(seen) == 2
        assert run.plan_status == "ok" and run.timing()["workers_ok"] == 2
        assert req.prompt.startswith("본 요청") and bw.RESULTS_HEAD in req.prompt
        assert "결론 : r1:w1" in req.prompt and "결론 : r1:w2" in req.prompt
        assert "PLAN_GATE" not in req.engine_env

    def test_실패한_갈래는_실패로_넘긴다(self) -> None:
        def invoke(req: EngineRequest) -> EngineResponse:
            if req.request_id.endswith("w2"):
                return EngineResponse(ok=False, body="", session_id=None, model_actual=None,
                                      elapsed=1.0, turns=None, usage=None, failure_reason="timeout")
            return _ok("결론 : 3건")

        req, run = bw.prepare(invoke, _request(), "요청", planner=self._planner())
        assert "갈래 2 (실패)" in req.prompt and "실행 실패 : timeout" in req.prompt
        assert run.timing()["workers_ok"] == 1

    @pytest.mark.parametrize(("status", "plan"), [("timeout", None), ("ok", {"type": "short", "branches": []})])
    def test_계획이_없거나_단답형이면_그대로_둔다(self, status: str, plan: Any) -> None:
        base = _request()
        req, run = bw.prepare(lambda r: pytest.fail("invoked"), base, "요청",
                              planner=self._planner(status, plan))
        assert req is base and run.plan_status == status and run.results == []

    def test_계획_호출_시간_초과는_timeout_이다(self) -> None:
        def slow(*_: Any) -> Any:
            raise subprocess.TimeoutExpired("claude", 20)

        assert bw.make_plan("요청", caller=slow)[0] == "timeout"


class TestPipeline:
    def test_켜면_작업자_뒤에_본_호출을_한다(self, tmp_path: Path, monkeypatch: Any) -> None:
        from test_pipeline import build_pipeline, make_ctx, ok_response

        monkeypatch.setattr(bw, "make_plan", lambda text, model=None: ("ok", _PLAN, 1.0))
        pipeline, deps = build_pipeline(
            responses=[ok_response(), ok_response(), ok_response()], tmp_path=tmp_path)
        pipeline._plan_gate_responses = 3
        pipeline._plan_workers = True
        pipeline.handle(make_ctx(channel="C1", user="UOWNER"))
        calls = deps["runner"].calls
        assert len(calls) == 3
        assert all(c.session_id is None for c in calls[:2])
        assert bw.RESULTS_HEAD in calls[2].prompt

    def test_끄면_본_호출만_한다(self, tmp_path: Path, monkeypatch: Any) -> None:
        from test_pipeline import build_pipeline, make_ctx, ok_response

        monkeypatch.setattr(bw, "make_plan", lambda *a, **k: pytest.fail("planned"))
        pipeline, deps = build_pipeline(responses=[ok_response()], tmp_path=tmp_path)
        pipeline._plan_gate_responses = 3
        pipeline.handle(make_ctx(channel="C1", user="UOWNER"))
        assert len(deps["runner"].calls) == 1
