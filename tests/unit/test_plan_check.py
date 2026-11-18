"""N6: 계획의 확인 기준(checks) 대비 점검. Stop 훅, 스위치 SCA_PLAN_INTAKE_CHECK."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_pipeline import build_pipeline, ok_response

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.engine.environment import ClaudeEnvironmentPolicy
from slack_cli_agent.guard import finish_hook
from slack_cli_agent.guard import finish_review as fr
from slack_cli_agent.guard.finish_review import StopDecision

CHECKS = [
    {"what": "배포 로그에 오류가 없다", "how": "로그 조회"},
    {"what": "건수가 12건이다", "how": "DB 조회"},
]


def _judge_output(results: list[dict[str, Any]]) -> str:
    return json.dumps({"type": "result", "structured_output": {"results": results}}, ensure_ascii=False)


def _runner(stdout: str = "", exc: Exception | None = None):
    calls: list[dict[str, Any]] = []

    def run(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    run.calls = calls  # type: ignore[attr-defined]
    return run


def _judge(runner, env=None) -> fr.PlanCheckJudge:
    return fr.PlanCheckJudge(runner=runner, executable="claude", env=env or {})


def _verdict(what: str, verdict: str, marked: bool = False) -> dict[str, Any]:
    return {"what": what, "verdict": verdict, "basis": "근거", "marked_unconfirmed": marked}


class TestJudge:
    def test_짧은_답도_점검한다(self):
        runner = _runner(_judge_output([_verdict(CHECKS[0]["what"], "충족")]))
        judge = _judge(runner)
        results = judge(CHECKS[:1], "네", "로그 정상")
        assert judge.status == "ok" and results and results[0]["verdict"] == "충족"
        call = runner.calls[0]
        assert call["cmd"][:4] == ["claude", "-p", "--model", "sonnet"]
        assert "--tools" in call["cmd"] and "--json-schema" in call["cmd"]
        assert call["env"][fr.VERIFY_NESTED_ENV] == "1"
        assert "배포 로그에 오류가 없다" in call["input"] and "로그 정상" in call["input"]

    def test_중첩_호출에서는_돌지_않는다(self):
        runner = _runner(_judge_output([]))
        judge = _judge(runner, env={fr.VERIFY_NESTED_ENV: "1"})
        assert judge(CHECKS, "a", "b") is None
        assert judge.status == "skip_nested" and not runner.calls

    @pytest.mark.parametrize(
        ("stdout", "exc", "status"),
        [
            ("", subprocess.TimeoutExpired("claude", 90), "error:TimeoutExpired"),
            ("모름", None, "error:no_json"),
            (json.dumps({"type": "result", "structured_output": {"x": 1}}), None, "error:bad_json"),
        ],
    )
    def test_실패는_None_과_상태(self, stdout, exc, status):
        judge = _judge(_runner(stdout, exc))
        assert judge(CHECKS, "a", "b") is None
        assert judge.status == status

    def test_모델_지정은_검증_모델_설정을_따른다(self):
        runner = _runner(_judge_output([]))
        fr.PlanCheckJudge(model="haiku", runner=runner, executable="claude", env={})(CHECKS, "a", "b")
        assert runner.calls[0]["cmd"][3] == "haiku"


class TestOutcome:
    def test_미충족은_차단하고_기준을_적는다(self):
        results = [_verdict("A", "미충족"), _verdict("B", "충족")]
        out = fr.plan_check_outcome(results, prior_keys=frozenset(), block_count=0, cap=1)
        assert out.reason and "A" in out.reason and "B" not in out.reason
        assert out.keys == {"A"}

    def test_확인불가는_표시_없으면_차단(self):
        out = fr.plan_check_outcome([_verdict("C", "확인불가")], prior_keys=frozenset(), block_count=0, cap=1)
        assert out.reason and "미확인" in out.reason

    def test_확인불가를_답이_이미_표시했으면_통과(self):
        out = fr.plan_check_outcome(
            [_verdict("C", "확인불가", marked=True)], prior_keys=frozenset(), block_count=0, cap=1,
        )
        assert out.reason is None

    def test_상한을_쓰면_차단하지_않는다(self):
        out = fr.plan_check_outcome([_verdict("A", "미충족")], prior_keys=frozenset(), block_count=1, cap=1)
        assert out.reason is None

    def test_이미_차단한_기준은_다시_차단하지_않는다(self):
        out = fr.plan_check_outcome([_verdict("A", "미충족")], prior_keys=frozenset({"A"}), block_count=1, cap=2)
        assert out.reason is None

    def test_모두_충족이면_통과(self):
        out = fr.plan_check_outcome([_verdict("A", "충족")], prior_keys=frozenset(), block_count=0, cap=1)
        assert out.reason is None

    def test_상한_환경변수(self):
        assert fr.resolve_plan_check_max_blocks({}) == 1
        assert fr.resolve_plan_check_max_blocks({fr.PLAN_CHECK_MAX_BLOCKS_ENV: "2"}) == 2
        assert fr.resolve_plan_check_max_blocks({fr.PLAN_CHECK_MAX_BLOCKS_ENV: "x"}) == 1

    def test_스위치(self):
        assert not fr.plan_check_enabled({})
        assert not fr.plan_check_enabled({fr.PLAN_CHECK_ENV: "0"})
        assert fr.plan_check_enabled({fr.PLAN_CHECK_ENV: "1"})


ANSWER = "네, 끝났습니다."


def _write_transcript(tmp_path: Path, checks: list | None = None) -> Path:
    checks = CHECKS if checks is None else checks
    path = tmp_path / "t.jsonl"
    rows = [
        {"type": "user", "uuid": "u1", "message": {"role": "user", "content": "배포하고 확인해줘"}},
        {"type": "assistant", "uuid": "a1", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "tail log"}}]}},
        {"type": "user", "uuid": "u2", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "오류 없음"}]}},
        {"type": "assistant", "uuid": "a2", "message": {"role": "assistant", "content": [
            {"type": "text", "text": ANSWER}]}},
    ]
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    Path(str(path) + ".plan.json").write_text(
        json.dumps({"status": "ok", "type": "short", "checks": checks}, ensure_ascii=False), encoding="utf-8",
    )
    return path


class _FakeJudge:
    def __init__(self, results):
        self.results, self.status, self.elapsed, self.calls = results, "ok", 0.1, 0

    def __call__(self, checks, answer, evidence):
        self.calls += 1
        return self.results


def _run_hook(monkeypatch, tmp_path, judge, *, on=True, decision="allow", cap=None, transcript=None):
    transcript = transcript or _write_transcript(tmp_path)
    for name in (fr.PLAN_CHECK_ENV, fr.PLAN_CHECK_MAX_BLOCKS_ENV):
        monkeypatch.delenv(name, raising=False)
    if on:
        monkeypatch.setenv(fr.PLAN_CHECK_ENV, "1")
    if cap:
        monkeypatch.setenv(fr.PLAN_CHECK_MAX_BLOCKS_ENV, str(cap))
    monkeypatch.setenv("SCA_FINISH_VERIFY", "0")
    monkeypatch.setenv("FINISH_HOOK_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(finish_hook, "decide", lambda *a, **k: StopDecision(action=decision))
    monkeypatch.setattr(finish_hook, "_make_plan_check_judge", lambda model: judge)
    payload = {"session_id": "s", "transcript_path": str(transcript), "last_assistant_message": ANSWER}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    finish_hook.main(["x"])
    text = out.getvalue().strip()
    return (json.loads(text) if text else {}), Path(str(transcript) + ".plan.checks.json")


UNMET = [_verdict("배포 로그에 오류가 없다", "충족"), _verdict("건수가 12건이다", "미충족")]


class TestHook:
    def test_꺼져_있으면_판정을_부르지_않고_출력도_같다(self, monkeypatch, tmp_path):
        judge = _FakeJudge(UNMET)
        out, checks_file = _run_hook(monkeypatch, tmp_path, judge, on=False)
        assert out == {} and judge.calls == 0 and not checks_file.exists()

    def test_미충족이면_한번_차단하고_기록한다(self, monkeypatch, tmp_path):
        judge = _FakeJudge(UNMET)
        out, checks_file = _run_hook(monkeypatch, tmp_path, judge)
        assert out["decision"] == "block" and "건수가 12건이다" in out["reason"]
        record = json.loads(checks_file.read_text(encoding="utf-8"))
        assert record["blocked"] is True and record["round"] == 1
        assert [r["verdict"] for r in record["results"]] == ["충족", "미충족"]

    def test_차단_뒤_재점검은_기록하되_다시_차단하지_않는다(self, monkeypatch, tmp_path):
        transcript = _write_transcript(tmp_path)
        _run_hook(monkeypatch, tmp_path, _FakeJudge(UNMET), transcript=transcript)
        judge = _FakeJudge([_verdict("건수가 12건이다", "미충족")])
        out, checks_file = _run_hook(monkeypatch, tmp_path, judge, transcript=transcript)
        assert out == {} and judge.calls == 1
        record = json.loads(checks_file.read_text(encoding="utf-8"))
        assert record["blocked"] is False and record["round"] == 2

    def test_다른_점검이_차단하면_이번엔_건너뛴다(self, monkeypatch, tmp_path):
        judge = _FakeJudge(UNMET)
        _, checks_file = _run_hook(monkeypatch, tmp_path, judge, decision="block")
        assert judge.calls == 0 and not checks_file.exists()

    def test_checks_가_없으면_건너뛴다(self, monkeypatch, tmp_path):
        judge = _FakeJudge(UNMET)
        transcript = _write_transcript(tmp_path, checks=[{"what": "", "how": ""}])
        out, _ = _run_hook(monkeypatch, tmp_path, judge, transcript=transcript)
        assert out == {} and judge.calls == 0

    def test_판정_실패는_차단하지_않고_상태를_남긴다(self, monkeypatch, tmp_path):
        judge = _FakeJudge(None)
        judge.status = "error:TimeoutExpired"
        out, checks_file = _run_hook(monkeypatch, tmp_path, judge)
        assert out == {}
        record = json.loads(checks_file.read_text(encoding="utf-8"))
        assert record["blocked"] is False and record["status"] == "error:TimeoutExpired"

    def test_상한은_verify_와_따로_세고_기준_문구로_중복을_거른다(self, monkeypatch, tmp_path):
        transcript = _write_transcript(tmp_path)
        _run_hook(monkeypatch, tmp_path, _FakeJudge(UNMET), transcript=transcript, cap=2)
        again, _ = _run_hook(monkeypatch, tmp_path, _FakeJudge(UNMET), transcript=transcript, cap=2)
        assert again == {}
        out, _ = _run_hook(
            monkeypatch, tmp_path, _FakeJudge([_verdict("새 기준", "미충족")]), transcript=transcript, cap=2,
        )
        assert out["decision"] == "block" and "새 기준" in out["reason"]


class TestWiring:
    def test_설정_기본값은_꺼짐(self):
        assert RuntimeSettings().plan_intake_check is False

    def test_엔진_환경_허용목록(self):
        allowed = ClaudeEnvironmentPolicy.ADDITIONAL_ALLOWED_VARS
        assert fr.PLAN_CHECK_ENV in allowed and fr.PLAN_CHECK_MAX_BLOCKS_ENV in allowed

    def test_환경은_intake_와_함께일_때만_싣는다(self, tmp_path):
        for intake, check, expected in [(True, True, "1"), (False, True, None), (True, False, None)]:
            pipeline, _ = build_pipeline(responses=[ok_response()], tmp_path=tmp_path)
            pipeline._plan_gate_responses = 3
            pipeline._plan_intake = intake
            pipeline._plan_intake_check = check
            assert pipeline._plan_gate_env("owner", None).get(fr.PLAN_CHECK_ENV) == expected
