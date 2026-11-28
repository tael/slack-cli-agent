"""guard/plan_steps: step ledger, approval gate, replan cap, step injection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from slack_cli_agent.guard import plan_start, plan_steps
from slack_cli_agent.guard.pretool import plan_gate
from slack_cli_agent.observability import progress_hook
from slack_cli_agent.observability.progress import ProgressLogReader

RAW = [
    {"do": "상품 페이지 열기", "tool": "browser", "approval": False},
    {"do": "수량 2 입력", "tool": "browser", "approval": False},
    {"do": "결제하기 클릭", "tool": "browser", "approval": True},
]


def _ledger(tmp_path: Path, turn: str = "C1:1.0") -> Path:
    transcript = str(tmp_path / "s.jsonl")
    path = plan_steps.ledger_path(transcript)
    plan_steps.create(path, plan_steps.clean_steps(RAW), request="상품권 2장 구매", turn=turn, skills=[])
    return path


def test_순서를_건너뛴_start_는_거부된다(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    code, out = plan_steps.run_command(path, ["start", "2"])
    assert code == 3 and "1단계가 끝나지 않았다" in out


def test_승인_단계는_승인_없이_시작되지_않는다(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    for n in ("1", "2"):
        plan_steps.run_command(path, ["start", n])
        plan_steps.run_command(path, ["done", n])
    code, out = plan_steps.run_command(path, ["start", "3"])
    assert code == 3 and "승인" in out


def test_계획한_턴과_비소유자_턴은_승인할_수_없다(tmp_path: Path) -> None:
    path = _ledger(tmp_path, turn="C1:1.0")
    for n in ("1", "2"):
        plan_steps.run_command(path, ["start", n])
        plan_steps.run_command(path, ["done", n])
    same = plan_steps.run_command(path, ["approve"], {"TURN_ID": "C1:1.0", "TURN_TIER": "owner"})
    other = plan_steps.run_command(path, ["approve"], {"TURN_ID": "C1:2.0", "TURN_TIER": "member"})
    ok = plan_steps.run_command(path, ["approve"], {"TURN_ID": "C1:2.0", "TURN_TIER": "owner"})
    assert same[0] == 3 and other[0] == 3 and ok[0] == 0
    assert plan_steps.run_command(path, ["start", "3"])[0] == 0


def test_게이트는_계획한_턴에서_승인_단계_앞의_쓰기_도구를_막는다(tmp_path: Path) -> None:
    path = _ledger(tmp_path, turn="C1:1.0")
    transcript = str(path).removesuffix(".plan.steps.json")
    data = {"transcript_path": transcript, "tool_name": "Bash", "tool_input": {"command": "node b.mjs click e1"}}
    env = {"TURN_ID": "C1:1.0"}
    assert plan_steps.gate(data, env) == ""  # step 1 does not need approval
    for n in ("1", "2"):
        plan_steps.run_command(path, ["start", n])
        plan_steps.run_command(path, ["done", n])
    assert "승인" in plan_steps.gate(data, env)
    assert plan_steps.gate({**data, "tool_name": "Read", "tool_input": {}}, env) == ""
    assert plan_steps.gate(data, {"TURN_ID": "C1:2.0"}) == ""  # a later turn may answer or approve


def test_승인_기록_없이는_approved_플래그를_막는다(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    transcript = str(path).removesuffix(".plan.steps.json")
    data = {"transcript_path": transcript, "tool_name": "Bash",
            "tool_input": {"command": "node browser.mjs click --text 결제하기 --approved"}}
    assert "--approved" in plan_steps.gate(data, {"TURN_ID": "C1:9.0"})


def test_실패하면_재계획하고_상한에서_멈춘다(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    ledger = plan_steps.read(path)
    assert ledger is not None
    fresh = lambda _prompt: plan_steps.clean_steps([{"do": "다른 경로로 열기", "tool": "browser", "approval": False}])  # noqa: E731
    for _ in range(plan_steps.MAX_REPLANS):
        step = plan_steps.next_step(ledger)
        assert step is not None
        out = plan_steps.cmd_fail(ledger, step["n"], "404", replanner=fresh)
        assert "다시 계획" in out and ledger["status"] == "active"
    step = plan_steps.next_step(ledger)
    assert step is not None
    out = plan_steps.cmd_fail(ledger, step["n"], "404", replanner=fresh)
    assert "상한" in out and ledger["status"] == "halted"


def test_스킬_목록을_frontmatter_에서_읽는다(tmp_path: Path) -> None:
    skill = tmp_path / "browser"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: browser\ndescription: 브라우저 조작\n---\n본문\n", encoding="utf-8")
    assert [(s["name"], s["description"]) for s in plan_steps.scan_skills(tmp_path)] == [("browser", "브라우저 조작")]


def test_계획_파일의_steps_가_장부가_되고_주입된다(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    transcript = str(tmp_path / "s.jsonl")
    monkeypatch.setenv("SCA_PLAN_STEPS", "1")
    monkeypatch.setenv("PLAN_GATE", "3")
    monkeypatch.setenv("TURN_ID", "C1:1.0")
    monkeypatch.setenv("SCA_PLAN_SKILLS", json.dumps([{"name": "browser", "description": "브라우저"}]))
    plan_start.prompt_path(transcript).write_text("상품권 2장 구매", encoding="utf-8")

    class Proc:
        stdout = json.dumps({"type": "result", "structured_output": {"type": "short", "branches": [], "steps": RAW}})

    record = plan_start.run(transcript, caller=lambda *a: Proc())
    assert record["status"] == "ok" and record["steps_ledger"] == "created:3"
    verdict, context = plan_gate.decide({"transcript_path": transcript})
    assert verdict == "inject"
    assert plan_gate.STEPS_HEAD in context and "[승인 필요]" in context
    assert "start N" in context and "--ledger" in context


def test_진행_표시는_실행_중_단계를_보여_준다(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    plan_steps.run_command(path, ["start", "1"])
    log = tmp_path / "progress.log"
    transcript = str(path).removesuffix(".plan.steps.json")
    progress_hook.record(log, json.dumps({"tool_name": "Bash", "transcript_path": transcript}))
    assert ProgressLogReader().read_new_labels(log) == ["1/3 상품 페이지 열기"]


def test_스킬_단계가_없는_계획은_장부를_만들지_않는다(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    transcript = str(tmp_path / "s.jsonl")
    monkeypatch.setenv("SCA_PLAN_SKILLS", json.dumps([{"name": "browser", "description": "브라우저"}]))
    monkeypatch.setenv("TURN_ID", "C1:1.0")
    raw = [
        {"do": "pytest 실행", "tool": "Bash", "approval": False},
        {"do": "결과 보고", "tool": "확인 필요", "approval": False},
    ]
    assert plan_start._write_ledger(transcript, raw, "시험 돌려 줘") == "none:no_skill"
    assert not plan_steps.ledger_path(transcript).exists()
    assert plan_start._write_ledger(transcript, RAW, "상품권 2장 구매") == "created:3"


def test_승인_대기_장부는_스킬_없는_새_계획에도_남는다(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _ledger(tmp_path)
    for n in ("1", "2"):
        plan_steps.run_command(path, ["start", n])
        plan_steps.run_command(path, ["done", n])
    transcript = str(path).removesuffix(".plan.steps.json")
    monkeypatch.setenv("SCA_PLAN_SKILLS", json.dumps([{"name": "browser", "description": "브라우저"}]))
    raw = [{"do": "승인 기록", "tool": "Bash", "approval": False}]
    assert plan_start._write_ledger(transcript, raw, "승인") == "kept_waiting"
    assert path.exists()


def test_executor_는_승인_단계_앞까지_한_번에_넘긴다() -> None:
    assert "한 번에 모두 넘긴다" in plan_gate.STEPS_EXECUTOR
    assert "한 번에 모두 넘긴다" in plan_gate.STEPS_EXECUTOR_RESUME


def test_승인_턴에서_approve_전_executor_호출은_막는다(tmp_path: Path) -> None:
    path = _ledger(tmp_path, turn="C1:1.0")
    for n in ("1", "2"):
        plan_steps.run_command(path, ["start", n])
        plan_steps.run_command(path, ["done", n])
    transcript = str(path).removesuffix(".plan.steps.json")
    call = {"transcript_path": transcript, "tool_name": "Agent", "tool_input": {"subagent_type": "executor"}}
    later = {"TURN_ID": "C1:2.0", "TURN_TIER": "owner"}
    assert "approve" in plan_steps.gate(call, later)
    lookup = {**call, "tool_input": {"subagent_type": "lookup"}}
    assert plan_steps.gate(lookup, later) == ""
    assert plan_steps.run_command(path, ["approve"], later)[0] == 0
    assert plan_steps.gate(call, later) == ""
