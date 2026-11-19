"""입출력 절차 v2 3단계: 계획의 output 대비 Stop 훅 점검 (SCA_PLAN_OUTPUT_CHECK)."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.engine.environment import ClaudeEnvironmentPolicy
from slack_cli_agent.guard import finish_hook
from slack_cli_agent.guard import finish_review as fr
from slack_cli_agent.guard.finish_review import StopDecision

ITEMS = ["요청 시각", "채널", "요청 요지", "소요 초", "성공 여부"]
GUESSES = [
    {"name": "고를 기준", "value": "처리 시간", "kind": "guess", "reason": "기준 미지정"},
    {"name": "대상 범위", "value": "실패 포함", "kind": "guess", "reason": "범위 미지정"},
]
GIVEN = {"name": "날짜", "value": "어제", "kind": "given", "reason": ""}
ASK = {"name": "대상", "value": "", "kind": "ask", "reason": "없음"}
ANSWER = "어제 최장 요청은 21:46 admin 채널의 '추천대로', 148.3초, 성공. 처리 시간 기준, 실패 포함"


def _plan(**over: Any) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "status": "ok", "intent": "최장 요청", "inputs": [GIVEN, *GUESSES], "ask": "", "ack": "x",
        "output": {"form": "한두줄", "items": ITEMS},
    }
    plan.update(over)
    return plan


def _verdict(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"missing_items": [], "unconfirmed_items": [], "extra": [], "undisclosed": []}
    base.update(over)
    return base


class TestSpec:
    def test_v2_계획에서_형태_항목_추정값을_뽑는다(self):
        spec = fr.plan_output_spec(_plan())
        assert spec == {"form": "한두줄", "items": ITEMS, "guesses": [
            {"name": "고를 기준", "value": "처리 시간"}, {"name": "대상 범위", "value": "실패 포함"},
        ]}

    @pytest.mark.parametrize("plan", [
        {"status": "ok", "checks": [{"what": "a", "how": "b"}]},
        _plan(status="pending"),
        _plan(status="failed"),
        _plan(inputs=[GIVEN, ASK]),
        _plan(output={"form": "없는형태", "items": ["a"]}),
        None,
        "x",
    ])
    def test_건너뛸_계획은_None(self, plan):
        assert fr.plan_output_spec(plan) is None

    def test_스위치_값(self):
        assert fr.plan_output_mode({}) == ""
        assert fr.plan_output_mode({fr.PLAN_OUTPUT_CHECK_ENV: "record"}) == "record"
        assert fr.plan_output_mode({fr.PLAN_OUTPUT_CHECK_ENV: " block "}) == "block"
        assert fr.plan_output_mode({fr.PLAN_OUTPUT_CHECK_ENV: "1"}) == ""


class TestForm:
    def test_한줄_답은_통과(self):
        assert fr.form_violation("한두줄", ANSWER) is None

    def test_불릿_줄이_있으면_한두줄_위반(self):
        answer = ANSWER + "\n- 시간 분해\n- 2위\n- 총 61건"
        assert fr.form_violation("한두줄", answer) == "한두줄"

    def test_번호_줄과_세_줄도_위반(self):
        assert fr.form_violation("한두줄", "a\n1. b") == "한두줄"
        assert fr.form_violation("한두줄", "a\nb\nc") == "한두줄"

    def test_답_끝_표식과_WATCH_줄은_세지_않는다(self):
        assert fr.form_violation("한두줄", ANSWER + "\n둘째 줄\n[[NOWATCH]]") is None
        assert fr.form_violation("한두줄", ANSWER + "\n둘째 줄\n[[WATCH: 내일 확인]]") is None

    def test_문단은_불릿만_막는다(self):
        assert fr.form_violation("문단", "가나다\n라마바") is None
        assert fr.form_violation("문단", "가나다\n* 라마바") == "문단"

    def test_목록_표_긴글은_코드로_보지_않는다(self):
        assert fr.form_violation("목록", "a\nb\nc\nd") is None
        assert fr.form_violation("표", "- a") is None


def _output(verdict: dict[str, Any]) -> str:
    return json.dumps({"type": "result", "structured_output": verdict}, ensure_ascii=False)


def _runner(stdout: str = "", exc: Exception | None = None):
    calls: list[dict[str, Any]] = []

    def run(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    run.calls = calls  # type: ignore[attr-defined]
    return run


def _judge(runner, env=None) -> fr.OutputCheckJudge:
    return fr.OutputCheckJudge(runner=runner, executable="claude", env=env or {})


def _spec(plan: dict[str, Any] | None = None) -> dict[str, Any]:
    spec = fr.plan_output_spec(plan or _plan())
    assert spec is not None
    return spec


class TestJudge:
    def test_입력은_형태_항목_추정값_답이고_도구근거는_없다(self):
        runner = _runner(_output(_verdict()))
        judge = _judge(runner)
        verdict = judge(_spec(), ANSWER)
        assert judge.status == "ok" and verdict == _verdict()
        call = runner.calls[0]
        assert call["cmd"][:3] == ["claude", "-p", "--model"] and "--json-schema" in call["cmd"]
        assert call["env"][fr.VERIFY_NESTED_ENV] == "1"
        for needle in ("한두줄", "요청 시각", "성공 여부", "고를 기준", "처리 시간", ANSWER):
            assert needle in call["input"]
        assert "[근거]" not in call["input"]

    def test_extra_는_세_개까지만(self):
        runner = _runner(_output(_verdict(extra=["a", "b", "c", "d"])))
        verdict = _judge(runner)(_spec(), ANSWER)
        assert verdict is not None and verdict["extra"] == ["a", "b", "c"]
        cmd = runner.calls[0]["cmd"]
        schema = json.loads(cmd[cmd.index("--json-schema") + 1])
        assert schema["properties"]["extra"]["maxItems"] == 3
        assert set(schema["required"]) == {"missing_items", "unconfirmed_items", "extra", "undisclosed"}

    def test_추정값이_없으면_undisclosed_는_비운다(self):
        runner = _runner(_output(_verdict(undisclosed=["x"])))
        verdict = _judge(runner)(_spec(_plan(inputs=[GIVEN])), ANSWER)
        assert verdict is not None and verdict["undisclosed"] == []

    def test_실패는_None_과_상태(self):
        judge = _judge(_runner(exc=subprocess.TimeoutExpired("c", 1)))
        assert judge(_spec(), ANSWER) is None and judge.status == "error:TimeoutExpired"
        judge = _judge(_runner("not json"))
        assert judge(_spec(), ANSWER) is None and judge.status == "error:no_json"
        judge = _judge(_runner(_output({"missing_items": "x"})))
        assert judge(_spec(), ANSWER) is None and judge.status == "error:bad_json"

    def test_중첩_호출은_건너뛴다(self):
        runner = _runner(_output(_verdict()))
        judge = _judge(runner, env={fr.VERIFY_NESTED_ENV: "1"})
        assert judge(_spec(), ANSWER) is None
        assert judge.status == "skip_nested" and not runner.calls


class TestOutcome:
    def test_모두_맞으면_차단_없음(self):
        assert fr.plan_output_outcome(_verdict(), None, "한두줄", block_count=0, cap=1).reason is None

    def test_사유_문구_전부(self):
        verdict = _verdict(
            missing_items=["소요 초"], unconfirmed_items=["채널"], extra=["2위", "총 건수"], undisclosed=["처리 시간"],
        )
        reason = fr.plan_output_outcome(verdict, "한두줄", "한두줄", block_count=0, cap=1).reason
        assert reason is not None
        for part in (
            "기대 결과에 있는데 답에 없는 항목: 소요 초",
            "미확인으로 남긴 항목은 도구로 찾아 채운다: 채널",
            "기대 결과에 없는 내용을 뺀다: 2위, 총 건수",
            "형태는 한두줄 이다",
            "추정한 값을 밝힌다: 처리 시간",
            "도구 결과가 기대 결과와 맞지 않으면 요청을 따르고 사유를 한 문장으로 적는다",
        ):
            assert part in reason

    def test_해당_없는_줄은_넣지_않는다(self):
        reason = fr.plan_output_outcome(_verdict(extra=["a"]), None, "표", block_count=0, cap=1).reason
        assert reason is not None and "형태는" not in reason and "추정한" not in reason

    def test_형태만_어긋나도_판정기_실패와_무관하게_차단(self):
        reason = fr.plan_output_outcome(None, "문단", "문단", block_count=0, cap=1).reason
        assert reason is not None and "형태는 문단 이다" in reason

    def test_판정기_실패와_형태_통과면_차단_없음(self):
        assert fr.plan_output_outcome(None, None, "문단", block_count=0, cap=1).reason is None

    def test_상한을_넘으면_차단하지_않는다(self):
        assert fr.plan_output_outcome(_verdict(extra=["a"]), None, "표", block_count=1, cap=1).reason is None
        assert fr.plan_output_outcome(_verdict(extra=["a"]), None, "표", block_count=1, cap=2).reason is not None


class _FakeJudge:
    def __init__(self, verdict):
        self.verdict, self.status, self.elapsed, self.calls = verdict, "ok", 0.1, 0

    def __call__(self, spec, answer):
        self.calls += 1
        return self.verdict


def _write_transcript(tmp_path: Path, plan: Any, answer: str = ANSWER, injected: bool = False) -> Path:
    path = tmp_path / "t.jsonl"
    rows = [
        {"type": "user", "uuid": "u1", "message": {"role": "user", "content": "어제 가장 오래 걸린 요청"}},
        {"type": "assistant", "uuid": "a1", "message": {"role": "assistant", "content": [
            {"type": "text", "text": answer}]}},
    ]
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    Path(str(path) + ".plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    if injected:
        Path(str(path) + ".plan.injected").write_text("1", encoding="utf-8")
    return path


def _run_hook(monkeypatch, tmp_path, judge, *, mode="block", plan=None, decision="allow", cap=None,
              transcript=None, injected=False, answer=ANSWER):
    transcript = transcript or _write_transcript(tmp_path, plan or _plan(), answer, injected)
    for name in (fr.PLAN_OUTPUT_CHECK_ENV, fr.PLAN_CHECK_MAX_BLOCKS_ENV, fr.PLAN_CHECK_ENV):
        monkeypatch.delenv(name, raising=False)
    if mode:
        monkeypatch.setenv(fr.PLAN_OUTPUT_CHECK_ENV, mode)
    if cap:
        monkeypatch.setenv(fr.PLAN_CHECK_MAX_BLOCKS_ENV, str(cap))
    monkeypatch.setenv("SCA_FINISH_VERIFY", "0")
    monkeypatch.setenv("FINISH_HOOK_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(finish_hook, "decide", lambda *a, **k: StopDecision(action=decision))
    monkeypatch.setattr(finish_hook, "_make_output_check_judge", lambda model: judge)
    spawned: list[Path] = []
    monkeypatch.setattr(finish_hook, "_spawn_output_judge", lambda job: spawned.append(job))
    payload = {"session_id": "s", "transcript_path": str(transcript), "last_assistant_message": answer}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    finish_hook.main(["x"])
    text = out.getvalue().strip()
    return (json.loads(text) if text else {}), Path(str(transcript) + ".plan.output.json"), spawned


BAD = _verdict(extra=["2위"])


class TestBlockMode:
    def test_불일치면_한번_차단하고_기록한다(self, monkeypatch, tmp_path):
        judge = _FakeJudge(BAD)
        out, record_file, spawned = _run_hook(monkeypatch, tmp_path, judge, injected=True)
        assert out["decision"] == "block" and "기대 결과에 없는 내용을 뺀다: 2위" in out["reason"]
        record = json.loads(record_file.read_text(encoding="utf-8"))
        assert record["blocked"] is True and record["injected"] is True and record["mode"] == "block"
        assert record["verdict"] == BAD and record["round"] == 1 and not spawned

    def test_일치하면_통과하고_기록은_남긴다(self, monkeypatch, tmp_path):
        out, record_file, _ = _run_hook(monkeypatch, tmp_path, _FakeJudge(_verdict()))
        record = json.loads(record_file.read_text(encoding="utf-8"))
        assert out == {} and record["blocked"] is False and record["injected"] is False

    def test_답이_형태를_어기면_판정기가_통과여도_차단(self, monkeypatch, tmp_path):
        out, _, _ = _run_hook(monkeypatch, tmp_path, _FakeJudge(_verdict()), answer=ANSWER + "\n- a\n- b")
        assert out["decision"] == "block" and "형태는 한두줄 이다" in out["reason"]

    def test_차단_뒤_재점검은_다시_차단하지_않는다(self, monkeypatch, tmp_path):
        transcript = _write_transcript(tmp_path, _plan())
        _run_hook(monkeypatch, tmp_path, _FakeJudge(BAD), transcript=transcript)
        out, record_file, _ = _run_hook(monkeypatch, tmp_path, _FakeJudge(BAD), transcript=transcript)
        assert out == {} and json.loads(record_file.read_text(encoding="utf-8"))["round"] == 2

    def test_상한은_환경변수를_따른다(self, monkeypatch, tmp_path):
        transcript = _write_transcript(tmp_path, _plan())
        _run_hook(monkeypatch, tmp_path, _FakeJudge(BAD), transcript=transcript, cap=2)
        out, _, _ = _run_hook(monkeypatch, tmp_path, _FakeJudge(BAD), transcript=transcript, cap=2)
        assert out["decision"] == "block"

    def test_판정기_실패는_차단하지_않는다(self, monkeypatch, tmp_path):
        judge = _FakeJudge(None)
        judge.status = "error:TimeoutExpired"
        out, record_file, _ = _run_hook(monkeypatch, tmp_path, judge)
        record = json.loads(record_file.read_text(encoding="utf-8"))
        assert out == {} and record["status"] == "error:TimeoutExpired"

    def test_로그_줄을_남긴다(self, monkeypatch, tmp_path):
        _run_hook(monkeypatch, tmp_path, _FakeJudge(BAD))
        lines = (tmp_path / "state" / "finish-hook-plan-output.jsonl").read_text(encoding="utf-8").splitlines()
        assert json.loads(lines[0])["blocked"] is True


class TestSkips:
    def test_꺼져_있으면_부르지_않는다(self, monkeypatch, tmp_path):
        judge = _FakeJudge(BAD)
        out, record_file, spawned = _run_hook(monkeypatch, tmp_path, judge, mode="")
        assert out == {} and judge.calls == 0 and not record_file.exists() and not spawned

    @pytest.mark.parametrize("plan", [
        {"status": "ok", "checks": [{"what": "a", "how": "b"}]},
        _plan(status="pending"),
        _plan(inputs=[GIVEN, ASK]),
    ])
    def test_v1_ask_pending_계획은_건너뛴다(self, monkeypatch, tmp_path, plan):
        judge = _FakeJudge(BAD)
        out, record_file, spawned = _run_hook(monkeypatch, tmp_path, judge, plan=plan)
        assert out == {} and judge.calls == 0 and not record_file.exists() and not spawned

    def test_다른_차단이_있으면_건너뛴다(self, monkeypatch, tmp_path):
        judge = _FakeJudge(BAD)
        _, record_file, _ = _run_hook(monkeypatch, tmp_path, judge, decision="block")
        assert judge.calls == 0 and not record_file.exists()


class TestRecordMode:
    def test_분리_프로세스를_띄우고_인라인_판정도_차단도_없다(self, monkeypatch, tmp_path):
        judge = _FakeJudge(BAD)
        out, _, spawned = _run_hook(monkeypatch, tmp_path, judge, mode="record", injected=True)
        assert out == {} and judge.calls == 0 and len(spawned) == 1
        job = json.loads(spawned[0].read_text(encoding="utf-8"))
        assert job["injected"] is True and job["answer"] == ANSWER and job["spec"]["form"] == "한두줄"
        assert job["transcript"].endswith("t.jsonl")

    def test_작업_파일을_받은_워커가_기록한다(self, monkeypatch, tmp_path):
        _, _, spawned = _run_hook(monkeypatch, tmp_path, _FakeJudge(BAD), mode="record")
        worker_judge = _FakeJudge(BAD)
        monkeypatch.setattr(finish_hook, "_make_output_check_judge", lambda model: worker_judge)
        assert finish_hook.main(["finish_hook.py", "--judge-output", str(spawned[0])]) == 0
        record_file = Path(str(tmp_path / "t.jsonl") + ".plan.output.json")
        record = json.loads(record_file.read_text(encoding="utf-8"))
        assert record["mode"] == "record" and record["would_block"] is True and record["blocked"] is False
        assert record["verdict"] == BAD and worker_judge.calls == 1
        assert not spawned[0].exists()
        log = tmp_path / "state" / "finish-hook-plan-output.jsonl"
        assert json.loads(log.read_text(encoding="utf-8").splitlines()[0])["mode"] == "record"

    def test_기록_모드는_다른_차단이_있어도_띄우지_않는다(self, monkeypatch, tmp_path):
        _, _, spawned = _run_hook(monkeypatch, tmp_path, _FakeJudge(BAD), mode="record", decision="block")
        assert not spawned


class TestWiring:
    def test_설정_기본값은_꺼짐(self):
        assert RuntimeSettings().plan_output_check == ""

    def test_엔진_환경_허용목록(self):
        assert fr.PLAN_OUTPUT_CHECK_ENV in ClaudeEnvironmentPolicy.ADDITIONAL_ALLOWED_VARS

    @pytest.mark.parametrize(("intake", "value", "expected"), [
        (True, "record", "record"), (True, "block", "block"), (True, "", None),
        (True, "bogus", None), (False, "block", None),
    ])
    def test_환경은_intake_와_함께일_때_record_block_만_싣는다(self, tmp_path, intake, value, expected):
        from test_pipeline import build_pipeline, ok_response

        pipeline, _ = build_pipeline(responses=[ok_response()], tmp_path=tmp_path)
        pipeline._plan_gate_responses = 3
        pipeline._plan_intake = intake
        pipeline._plan_output_check = value
        assert pipeline._plan_gate_env("owner", None).get(fr.PLAN_OUTPUT_CHECK_ENV) == expected
