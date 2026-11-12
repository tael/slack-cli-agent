from slack_cli_agent.guard import finish_hook
from slack_cli_agent.guard.finish_review import carried_over, draft_key


def test_drafts_round_trip_and_lookup(tmp_path):
    path = finish_hook._drafts_path(tmp_path, "s-1")
    assert finish_hook._read_drafts(path) == ()

    finish_hook._write_drafts(path, (draft_key("원천은 DATAMART.ORDERS 이고\n건수는 12건임"),))

    drafts = finish_hook._read_drafts(path)
    assert carried_over("원천은 DATAMART.ORDERS 이고 건수는 12건임 (x)", drafts)


def test_bad_drafts_file_reads_empty(tmp_path):
    path = finish_hook._drafts_path(tmp_path, "s-2")
    path.write_text("{", encoding="utf-8")

    assert finish_hook._read_drafts(path) == ()


def _run_hook(monkeypatch, tmp_path, action, verdict):
    """finish_hook.main with decide() stubbed to *action* and the corrector to *verdict*."""
    import io
    import json

    from slack_cli_agent.guard import figurative
    from slack_cli_agent.guard.finish_review import StopDecision as Decision

    out = tmp_path / "fig.json"
    answer = "이번 배치는 정산 파이프라인과 맞닿은 지점에서 실패했습니다. " * 3
    monkeypatch.setenv(figurative.OUT_ENV, str(out))
    monkeypatch.setenv(figurative.MODE_ENV, "work")
    monkeypatch.setenv("SCA_FINISH_VERIFY", "0")
    seen = {}

    class Fake:
        def __call__(self, text, *, work):
            return verdict

        def cancel(self):
            seen["cancelled"] = True

    real_start = figurative.Precompute.start
    monkeypatch.setattr(finish_hook.Precompute, "start",
                        classmethod(lambda cls, a, env: real_start(a, env, corrector=Fake())))
    monkeypatch.setattr(finish_hook, "decide", lambda *a, **k: Decision(action=action))
    payload = {"session_id": "s", "transcript_path": "", "last_assistant_message": answer}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    finish_hook.main(["x"])
    return out, seen


def test_hook_writes_figurative_verdict_when_turn_ends(monkeypatch, tmp_path):
    out, _ = _run_hook(monkeypatch, tmp_path, "allow", {"changed": False})
    import json
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["mode"] == "work" and record["verdict"] == {"changed": False}


def test_hook_drops_figurative_call_when_verify_blocks(monkeypatch, tmp_path):
    out, seen = _run_hook(monkeypatch, tmp_path, "block", {"changed": False})
    assert not out.exists() and seen.get("cancelled")
