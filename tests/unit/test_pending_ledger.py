"""미완 항목 장부와 그에 기대는 종료 판정 시험.

2026-11-26 신지 스레드 : 답에 "남은 일" 을 남기거나 "정해 주면 진행함" 으로 결정을
넘기고 턴을 끝냈다. 소유자가 추궁할 때마다 한 단계씩만 움직였고, 정규식 종료 판정은
그 문장 셋을 하나도 잡지 못했다.
"""

from __future__ import annotations

import io
import json
import time

from slack_cli_agent.guard import finish_review, pending_ledger
from slack_cli_agent.guard.finish_review import ToolCall, Turn

SHINJI_BLOCKER = (
    "볼트 노트를 읽으려 하면 iCloud 미다운로드로 Read 가 EDEADLK 로 실패하고, 내려받는 brctl download 는 "
    "Bash 가드가 볼트 경로라며 거부해서 막혔음. 가드 예외나 우회 허용 여부를 정해 주면 진행함."
)
SHINJI_LEFT_LIST = (
    "남은 일 3건임.\n\n- 단방향 링크 38개 : 역링크 추가\n- 고립 노트 19개 : 관련 노트 연결\n"
    "- 시범 검증 : 질문이 인접 목록을 따라가는지 확인"
)
SHINJI_LEFT_TAIL = (
    "맞음. 직전 턴은 규칙 기록까지만 했고, 이번 턴에 인접 목록 스크립트를 추가해 조회로 확인함. "
    "남은 일은 단방향 링크 38개와 고립 노트 19개의 역링크 정리임."
)
DENIED = ToolCall(name="Bash", input={"command": "brctl download x"}, result="Permission to use Bash has been denied")


def _owner(answer: str, **kwargs) -> Turn:
    return Turn(answer=answer, is_owner_turn=True, **kwargs)


class TestLeftHit:
    def test_catches_the_three_shinji_sentences(self):
        for answer in (SHINJI_BLOCKER, SHINJI_LEFT_LIST, SHINJI_LEFT_TAIL):
            assert pending_ledger.left_hit(answer) is not None, answer

    def test_ignores_finished_or_unrelated_wording(self):
        for answer in (
            "남은 일은 내가 처리했음. 단방향 링크는 38개에서 0개가 됐음.",
            "남은 일 없음.",
            "남은 작업은 모두 끝냈다.",
            "## 남은 위험\n- 캐시가 오래되면 값이 어긋날 수 있다",
            '신지는 "남은 일 3건임." 이라고만 답했다.',
        ):
            assert pending_ledger.left_hit(answer) is None, answer

    def test_catches_not_started_steps(self):
        assert pending_ledger.left_hit("아직 시작하지 않은 단계는 링크 반영, 스크립트 작성 두 가지입니다.")


class TestLeftPendingJudgment:
    def test_blocks_owner_turn(self):
        turn = _owner(SHINJI_BLOCKER)
        assert finish_review.left_pending(turn) is not None
        assert finish_review.decide(turn).action == "block"

    def test_ask_tag_exempts(self):
        assert finish_review.left_pending(_owner(SHINJI_BLOCKER + "\n[[ASK]]")) is None

    def test_other_tiers_untouched(self):
        assert finish_review.left_pending(Turn(answer=SHINJI_BLOCKER)) is None


class TestExtract:
    def test_takes_marker_line_and_its_bullets(self):
        items = pending_ledger.extract(SHINJI_LEFT_LIST)
        assert items[0].startswith("남은 일 3건임")
        assert len(items) == 4
        assert items[-1].startswith("시범 검증")

    def test_nothing_pending_gives_empty(self):
        assert pending_ledger.extract("모두 처리했고 시험 236개가 통과함.") == ()


class TestLedgerFile:
    def test_round_trip_and_clear(self, tmp_path):
        pending_ledger.write(tmp_path, "s-1", ("남은 일 1건", "링크 정리"))
        assert pending_ledger.read(tmp_path, "s-1") == ("남은 일 1건", "링크 정리")
        pending_ledger.write(tmp_path, "s-1", ())
        assert pending_ledger.read(tmp_path, "s-1") == ()

    def test_stale_entry_is_ignored(self, tmp_path):
        pending_ledger.write(tmp_path, "s-2", ("남은 일",), now=time.time() - pending_ledger.TTL_SEC - 5)
        assert pending_ledger.read(tmp_path, "s-2") == ()

    def test_bad_file_reads_empty(self, tmp_path):
        pending_ledger.ledger_path(tmp_path, "s-3").write_text("{", encoding="utf-8")
        assert pending_ledger.read(tmp_path, "s-3") == ()

    def test_context_text_lists_items(self):
        text = pending_ledger.context_text(("남은 일 2건", "- 링크 정리"))
        assert "미완 항목 2건" in text and "링크 정리" in text
        assert pending_ledger.context_text(()) == ""


class TestLedgerUnworked:
    def test_blocks_toolless_turn_with_open_ledger(self):
        turn = _owner("남은 일은 세 가지입니다.", pending_ledger=("남은 일 3건임",))
        assert finish_review.ledger_unworked(turn) is not None

    def test_blocks_doc_only_pledge_turn(self):
        calls = (
            ToolCall(name="Write", input={"file_path": "/u/.shinji/persona/knowledge/_가드막힘.md"}, result="ok"),
            ToolCall(name="Read", input={"file_path": "/u/.shinji/persona/knowledge/_가드막힘.md"}, result="..."),
        )
        turn = _owner("다짐을 규칙으로 적었음.", pending_ledger=("남은 일 3건",), tool_calls=calls)
        assert finish_review.ledger_unworked(turn) is not None

    def test_real_work_passes(self):
        calls = (ToolCall(name="Bash", input={"command": "python3 build_adjacency.py"}, result="ok"),)
        turn = _owner("링크 38개를 정리했다.", pending_ledger=("남은 일 3건",), tool_calls=calls)
        assert finish_review.ledger_unworked(turn) is None

    def test_empty_ledger_or_ask_passes(self):
        assert finish_review.ledger_unworked(_owner("네.")) is None
        assert finish_review.ledger_unworked(_owner("승인 필요.\n[[ASK]]", pending_ledger=("남은 일",))) is None

    def test_blocks_only_once(self):
        turn = _owner("네, 확인했습니다.", pending_ledger=("남은 일 3건임",))
        assert finish_review.decide(turn).action == "block"
        assert finish_review.decide(turn, block_count=1).action == "allow"


class TestHandoffFullAuthority:
    ANSWER = "이 턴 권한 밖이라 실행할 수 없습니다. 담당자에게 요청해 주세요."

    def test_denial_still_exempts_ordinary_turn(self):
        assert finish_review.handoff(Turn(answer=self.ANSWER, tool_calls=(DENIED,))) is None

    def test_denial_does_not_exempt_full_authority_turn(self):
        turn = Turn(answer=self.ANSWER, tool_calls=(DENIED,), is_full_authority=True)
        assert finish_review.handoff(turn) is not None


class TestPlanStartInjection:
    def test_prints_ledger_on_owner_turn(self, tmp_path, monkeypatch, capsys):
        from slack_cli_agent.guard import plan_start

        transcript = tmp_path / "t.jsonl"
        transcript.write_text("", encoding="utf-8")
        pending_ledger.write(tmp_path, "s-9", ("남은 일 1건", "링크 정리"))
        monkeypatch.setenv("TURN_TIER", "owner_trusted")
        monkeypatch.delenv("FINISH_HOOK_STATE_DIR", raising=False)
        monkeypatch.setattr(plan_start, "start", lambda data: "skip")
        monkeypatch.setattr(
            "sys.stdin", io.StringIO(json.dumps({"session_id": "s-9", "transcript_path": str(transcript)}))
        )
        plan_start.main(["plan_start"])
        out = json.loads(capsys.readouterr().out)
        assert "링크 정리" in out["hookSpecificOutput"]["additionalContext"]

    def test_silent_without_ledger(self, tmp_path, monkeypatch, capsys):
        from slack_cli_agent.guard import plan_start

        transcript = tmp_path / "t.jsonl"
        transcript.write_text("", encoding="utf-8")
        monkeypatch.setenv("TURN_TIER", "owner_trusted")
        monkeypatch.setattr(plan_start, "start", lambda data: "skip")
        monkeypatch.setattr(
            "sys.stdin", io.StringIO(json.dumps({"session_id": "s-0", "transcript_path": str(transcript)}))
        )
        plan_start.main(["plan_start"])
        assert capsys.readouterr().out == ""


def _runner(payload: dict):
    import subprocess

    calls: list[str] = []

    def run(cmd, *, input, capture_output, text, timeout, env):  # noqa: A002
        calls.append(input)
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload, ensure_ascii=False), "")

    return run, calls


class TestOnlineLeftJudge:
    def test_verify_call_carries_left_question_and_parses_it(self):
        run, calls = _runner({"unsupported": [], "left": [{"sentence": "정해 주면 진행함.", "why": "넘김"}]})
        v = finish_review.ModelClaimVerifier(runner=run, executable="claude", env={}, ask_left=True)
        assert v("답", "근거") == []
        assert v.left == ["정해 주면 진행함."]
        assert '"left"' in calls[0]

    def test_plain_verify_ignores_left(self):
        run, calls = _runner({"unsupported": [], "left": [{"sentence": "x", "why": "y"}]})
        v = finish_review.ModelClaimVerifier(runner=run, executable="claude", env={})
        v("답", "근거")
        assert v.left == [] and '"left"' not in calls[0]

    def test_decide_blocks_once_on_model_left(self):
        run, _ = _runner({"unsupported": [], "left": [{"sentence": "링크 정리는 다음에 함.", "why": "남김"}]})
        answer = "단방향 링크 37개를 고쳤고 인접 목록을 다시 만들었으며 시험 236개가 통과함. " * 12
        answer += "링크 정리는 다음에 함."
        turn = _owner(answer, tool_calls=(ToolCall(name="Bash", input={"command": "pytest"}, result="236 passed"),))
        make = lambda: finish_review.ModelClaimVerifier(runner=run, executable="claude", env={}, ask_left=True)  # noqa: E731
        first = finish_review.decide(turn, claim_verifier=make())
        assert first.action == "block" and any(f.name == "left_model" for f in first.findings)
        assert finish_review.decide(turn, claim_verifier=make(), block_count=1).action == "allow"

    def test_detached_job_merges_into_ledger(self, tmp_path):
        from slack_cli_agent.guard import finish_hook

        run, calls = _runner({"left": [{"sentence": "시범 검증은 아직임.", "why": "남김"}]})
        pending_ledger.write(tmp_path, "s-5", ("남은 일 1건",))
        job = tmp_path / "job.json"
        job.write_text(json.dumps({"state_dir": str(tmp_path), "session": "s-5", "answer": "a", "model": "sonnet"}))
        judge = finish_review.ModelClaimVerifier(runner=run, executable="claude", env={}, left_only=True)
        finish_hook._judge_left_job(job, verifier=judge)
        assert pending_ledger.read(tmp_path, "s-5") == ("남은 일 1건", "시범 검증은 아직임.")
        assert not job.exists() and "[근거]" not in calls[0]
