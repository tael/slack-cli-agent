"""오프라인 그림자 평가기 (G5, mametchi shadow_eval.py).

원본 채널 질문에 실제 그림자 답과 같은 프롬프트로 답하고 실제 답과 대조한
결과를 파일로만 남긴다. 슬랙에는 아무것도 게시하지 않는다.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from slack_cli_agent.core.timezones import KST
from slack_cli_agent.shadow.offline import OfflineEvaluator, read_only, write_results
from slack_cli_agent.shadow.service import TurnResult

SOURCE, TARGET = "CSRC", "CSHADOW"
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=KST).timestamp()
OLD_TS = f"{NOW - 3 * 86400:.6f}"
FRESH_TS = f"{NOW - 3600:.6f}"


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.history = [
            {"ts": OLD_TS, "user": "U1", "text": "계정 신청 어디서 해요?", "reply_count": 2},
            {"ts": FRESH_TS, "user": "U1", "text": "방금 질문", "reply_count": 1},
            {"ts": f"{NOW - 2 * 86400:.6f}", "user": "U2", "text": "답 없는 질문"},
            {"ts": f"{NOW - 2 * 86400 + 1:.6f}", "bot_id": "B1", "text": "봇 글", "reply_count": 3},
        ]

    def __call__(self, method: str, **params: Any) -> dict[str, Any]:
        self.calls.append((method, params))
        if method == "conversations.history":
            if params.get("latest") and params.get("inclusive"):
                return {"messages": [m for m in self.history if m["ts"] == params["latest"]]}
            lo, hi = float(params["oldest"]), float(params["latest"])
            return {"messages": [m for m in self.history if lo <= float(m["ts"]) <= hi]}
        if method == "conversations.replies":
            return {"messages": [
                {"ts": params["ts"], "user": "U1", "text": "질문"},
                {"ts": "2.0", "user": "U9", "text": "옴니이솔에서 신청하세요"},
            ]}
        if method == "conversations.info":
            return {"channel": {"name": "people-support"}}
        raise AssertionError(f"예상하지 않은 호출 {method}")


def _evaluator(api: FakeApi, turns: list[TurnResult], prompts: list[str]) -> OfflineEvaluator:
    def run_turn(prompt: str, channel: str, request_id: str) -> TurnResult:
        assert channel == TARGET
        prompts.append(prompt)
        return turns.pop(0)

    return OfflineEvaluator(
        source_api=read_only(api), run_turn=run_turn, name_of=lambda u: f"이름{u}",
        bot_name="구치파치", source=SOURCE, target=TARGET, now=lambda: NOW,
    )


def test_questions_are_answered_person_posts_older_than_a_day() -> None:
    api = FakeApi()
    msgs = _evaluator(api, [], []).questions(days=14, limit=12)
    assert [m["ts"] for m in msgs] == [OLD_TS]


def test_questions_by_ts_fetch_each_message() -> None:
    api = FakeApi()
    msgs = _evaluator(api, [], []).questions(days=14, limit=12, only_ts=[FRESH_TS])
    assert [m["ts"] for m in msgs] == [FRESH_TS]


def test_evaluate_answers_blind_and_judges_against_the_real_answer() -> None:
    api = FakeApi()
    prompts: list[str] = []
    verdict = {"content": "맞음", "tone": "비슷함", "actual": "옴니이솔", "diff": "", "missing": ""}
    turns = [
        TurnResult(ok=True, body="옴니이솔에서 신청해요 [[RECIPIENT:ASKER]]",
                   tool_calls=(("slack_search", '{"query": "계정 before:2026-11-01"}'),)),
        TurnResult(ok=True, body="결과\n" + json.dumps(verdict, ensure_ascii=False), tool_calls=()),
    ]
    ev = _evaluator(api, turns, prompts)
    result = ev.evaluate(api.history[0])

    assert "블라인드 규칙" in prompts[0] and "people-support" in prompts[0]
    assert "옴니이솔에서 신청하세요" in prompts[1]
    assert result["answer"].strip() == "옴니이솔에서 신청해요"
    assert result["ok"] is True and result["blind_checked"] is True
    assert result["violations"] == []
    assert result["verdict"]["content"] == "맞음"
    assert "[이름U9]" in result["actual"]


def test_evaluate_reports_blind_violation_and_skips_judge_on_failure() -> None:
    api = FakeApi()
    turns = [TurnResult(ok=True, body="답", tool_calls=(("read", f"{SOURCE} {OLD_TS}"),)),
             TurnResult(ok=True, body="판정 형식 아님", tool_calls=())]
    result = _evaluator(api, turns, []).evaluate(api.history[0])
    assert result["violations"]
    assert result["verdict"]["content"] == "판정 실패"

    failed = _evaluator(FakeApi(), [TurnResult(ok=False, body="", tool_calls=None)], []).evaluate(api.history[0])
    assert failed["ok"] is False and failed["verdict"] == {}


def test_read_only_rejects_any_write_method() -> None:
    api = FakeApi()
    guarded = read_only(api)
    for method in ("chat.postMessage", "chat.update", "reactions.add", "files.upload"):
        with pytest.raises(PermissionError):
            guarded(method, channel=TARGET, text="x")
    assert api.calls == []


def test_run_writes_file_only_and_reads_only(tmp_path: Path) -> None:
    api = FakeApi()
    turns = [TurnResult(ok=True, body="답", tool_calls=()),
             TurnResult(ok=True, body='{"content": "틀림", "tone": "다름"}', tool_calls=())]
    ev = _evaluator(api, turns, [])
    results = ev.run(ev.questions(days=14, limit=12), workers=2)
    path = write_results(results, tmp_path, "20261107-120000")

    assert path == tmp_path / "20261107-120000.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved[0]["verdict"]["content"] == "틀림"
    assert {m for m, _ in api.calls} <= {"conversations.history", "conversations.replies", "conversations.info"}
