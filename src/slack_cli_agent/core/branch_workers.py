"""Engine-run branch workers (orchestration step C, 2026-11-11).

W5 (2026-11-10): with the flow note, the plan hook and agent definitions all
in place, the model handed a branch to an Agent worker in 1 of 6 branching
runs. W6 (2026-11-11): handing the model a ready plan did not raise parallel
calls either (40 vs 38 responses with 2+ calls, tasks/guchipachi-orchestration
/w6_engine_planner.md). So the branching itself moves into code here.

On a flow turn with ``plan_workers`` on, the pipeline asks the planner
(guard/plan_start, tool-less ``claude -p``) for a plan before the main call.
When the plan is ``branch`` with two or more independent branches, each of
those runs as its own engine call at the same time, with the turn's system
prompt and tools, a one-branch prompt and no session. Their results go into
the main call's prompt and the main call writes the answer.

A planner failure, a ``short``/``explore`` plan or fewer than two independent
branches leaves the turn as it was. A failed worker is passed on as failed;
nothing is filled in for it.
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any

from ..engine.base import EngineRequest, EngineResponse
from ..guard import plan_start

log = logging.getLogger(__name__)

#: Workers that run at once. The planner returns at most five branches.
MAX_WORKERS = 4
#: Worker answer characters carried into the main call's prompt.
RESULT_CHARS = 4000
#: Env the workers must not carry: no plan injection, no nested planner.
_DROP_ENV = (plan_start.PLAN_GATE_ENV,)

WORKER_NOTE = (
    "[작업자 지시] 아래 갈래 하나만 조사한다. 원래 요청 전체에 답하지 않는다.\n"
    "- 원래 요청 : {request}\n"
    "- 갈래 : 목표 : {goal} / 원천 : {source} / 반환 : {returns}\n"
    "- 조회만 한다. 파일, 코드, 설정을 고치지 않고 슬랙, 노션, 지라에 쓰거나 게시하지 않는다\n"
    "- 다른 갈래는 다른 작업자가 맡는다. 이 갈래 밖의 조회를 하지 않는다\n"
    "- 아래 형식으로만 답한다\n"
    "결론 : <한두 문장>\n"
    "근거 : <확인한 원천, 수치, 경로, 쿼리>\n"
    "실패한 조회 : <없으면 없음>"
)

RESULTS_HEAD = (
    "[작업자 결과] 아래 갈래는 작업자가 이미 조사했다. 이 결과를 근거로 답한다. "
    "같은 조회를 다시 하지 않는다. 실패했거나 부족한 갈래만 직접 조회한다. "
    "답변에 작업자, 갈래, 이 안내를 언급하지 않는다."
)


@dataclass(frozen=True)
class BranchResult:
    index: int
    goal: str
    ok: bool
    body: str
    elapsed: float
    response: EngineResponse | None = None


@dataclass
class WorkerRun:
    """What one turn's worker step did, for the audit row."""

    plan_status: str = "not_run"
    plan_type: str = ""
    plan_sec: float = 0.0
    results: list[BranchResult] = field(default_factory=list)
    workers_sec: float = 0.0

    def timing(self) -> dict[str, Any]:
        return {
            "plan_status": self.plan_status,
            "plan_type": self.plan_type,
            "plan_sec": round(self.plan_sec, 1),
            "workers": len(self.results),
            "workers_ok": sum(1 for r in self.results if r.ok),
            "workers_sec": round(self.workers_sec, 1),
            "workers_tokens": sum(_tokens(r.response) for r in self.results),
        }


def _tokens(response: EngineResponse | None) -> int:
    if response is None or not response.model_usage:
        return 0
    total = 0
    for row in response.model_usage.values():
        for key in ("input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens"):
            total += int(getattr(row, key, 0) or 0)
    return total


def make_plan(
    text: str, *, model: str = plan_start.DEFAULT_MODEL,
    caller: Callable[[str, str, float], subprocess.CompletedProcess[str]] | None = None,
) -> tuple[str, dict[str, Any] | None, float]:
    """(status, plan, seconds) from the tool-less planner."""
    started = time.monotonic()
    try:
        proc = (caller or plan_start._call_planner)(
            text[-plan_start.PROMPT_TAIL_CHARS:], model, plan_start.TIMEOUT_SEC,
        )
        status, plan = plan_start.parse(proc.stdout or "")
    except subprocess.TimeoutExpired:
        status, plan = "timeout", None
    except Exception as exc:  # noqa: BLE001 - recorded, the turn goes on without workers
        status, plan = "error:" + type(exc).__name__, None
    return status, plan, time.monotonic() - started


def independent_branches(plan: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Branches a worker can take: a ``branch`` plan's branches with no
    dependency, only when there are two or more of them."""
    if not plan or plan.get("type") != "branch":
        return []
    picked = [
        b for b in plan.get("branches") or []
        if isinstance(b, dict) and not b.get("depends_on") and str(b.get("goal", "")).strip()
    ]
    return picked if len(picked) >= 2 else []


def worker_request(base: EngineRequest, branch: Mapping[str, Any], index: int,
                   request_text: str, model: str | None) -> EngineRequest:
    env = {k: v for k, v in base.engine_env.items() if k not in _DROP_ENV}
    # The answer is not posted, so the model verify of the Stop hook only costs time.
    env["SCA_FINISH_VERIFY"] = "0"
    prompt = WORKER_NOTE.format(
        request=" ".join(request_text.split())[:1500],
        goal=branch.get("goal", ""), source=branch.get("source", ""),
        returns=branch.get("returns", ""),
    )
    return replace(
        base, prompt=prompt, session_id=None, resume=False, model=model or base.model,
        progress_log=None, engine_env=env,
        request_id=f"{base.request_id}:w{index}" if base.request_id else "",
    )


def run_workers(invoke: Callable[[EngineRequest], EngineResponse], base: EngineRequest,
                branches: Sequence[Mapping[str, Any]], request_text: str,
                model: str | None) -> list[BranchResult]:
    def one(index: int, branch: Mapping[str, Any]) -> BranchResult:
        started = time.monotonic()
        try:
            response = invoke(worker_request(base, branch, index, request_text, model))
        except Exception as exc:  # noqa: BLE001 - a failed branch is reported as failed
            log.warning("작업자 %d 실패 : %s", index, exc)
            return BranchResult(index, str(branch.get("goal", "")), False,
                                f"실행 실패 : {type(exc).__name__}", time.monotonic() - started)
        ok = bool(response.ok and response.body.strip())
        body = response.body if ok else f"실행 실패 : {response.failure_reason or '빈 응답'}"
        return BranchResult(index, str(branch.get("goal", "")), ok, body,
                            time.monotonic() - started, response)

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(branches))) as pool:
        futures = [pool.submit(one, i, b) for i, b in enumerate(branches, start=1)]
        return [f.result() for f in futures]


def results_note(results: Sequence[BranchResult]) -> str:
    lines = [RESULTS_HEAD]
    for r in results:
        state = "성공" if r.ok else "실패"
        body = r.body.strip()
        if len(body) > RESULT_CHARS:
            body = body[:RESULT_CHARS] + " ...(뒤 생략)"
        lines.append(f"\n갈래 {r.index} ({state}) 목표 : {r.goal}\n{body}")
    return "\n".join(lines)


def drop_plan_gate(env: Mapping[str, str]) -> dict[str, str]:
    """The main call's env once workers ran: its plan is already carried out."""
    return {k: v for k, v in env.items() if k != plan_start.PLAN_GATE_ENV}


def prepare(
    invoke: Callable[[EngineRequest], EngineResponse], request: EngineRequest, request_text: str,
    *, planner_model: str = plan_start.DEFAULT_MODEL, worker_model: str | None = None,
    planner: Callable[..., tuple[str, dict[str, Any] | None, float]] | None = None,
) -> tuple[EngineRequest, WorkerRun]:
    """Plan, run independent branches as workers, and return the main
    request with their results. Returns the request unchanged otherwise."""
    run = WorkerRun()
    status, plan, seconds = (planner or make_plan)(request_text, model=planner_model)
    run.plan_status, run.plan_sec = status, seconds
    run.plan_type = str((plan or {}).get("type") or "")
    branches = independent_branches(plan)
    if not branches:
        return request, run
    started = time.monotonic()
    run.results = run_workers(invoke, request, branches, request_text, worker_model)
    run.workers_sec = time.monotonic() - started
    note = results_note(run.results)
    return replace(request, prompt=f"{request.prompt}\n\n{note}",
                   engine_env=drop_plan_gate(request.engine_env)), run
