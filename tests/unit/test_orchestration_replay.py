"""기준선 재생 도구 tools/orchestration_replay.py 의 집계 규칙을 고정한다."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

SPEC = importlib.util.spec_from_file_location(
    "orchestration_replay", Path(__file__).resolve().parents[2] / "tools" / "orchestration_replay.py"
)
assert SPEC and SPEC.loader
replay = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = replay
SPEC.loader.exec_module(replay)


def line(kind: str, ts: str, content: Any, mid: str | None = None, **extra: Any) -> str:
    message: dict[str, Any] = {"content": content}
    if mid:
        message["id"] = mid
    return json.dumps({"type": kind, "timestamp": ts, "message": message, **extra}, ensure_ascii=False)


def use(name: str, **tool_input: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"t-{name}", "name": name, "input": tool_input}


def write(path: Path, lines: list[str]) -> str:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def small_session(tmp_path: Path) -> str:
    return write(tmp_path / "aaaaaaaa-1.jsonl", [
        line("user", "2026-11-08T01:00:00Z", "[01:00:00 김태일 <@U1>] 1,2,3 지역을 각각 비교해"),
        # one response = one message.id, written as two lines
        line("assistant", "2026-11-08T01:00:01Z", [use("Bash", command="ls")], "m1"),
        line("assistant", "2026-11-08T01:00:01Z", [use("Read", file_path="/a/b/c/d/e.py")], "m1"),
        line("user", "2026-11-08T01:00:02Z", [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]),
        line("assistant", "2026-11-08T01:00:03Z", [
            {"type": "text", "text": "[계획]\n- 갈래 1"}, use("Bash", command="ls"),
        ], "m2"),
        line("assistant", "2026-11-08T01:00:03Z", [{"type": "text", "text": "[계획]\n- 갈래 1"}], "m2"),
        line("user", "2026-11-08T01:00:04Z", [
            {"type": "tool_result", "tool_use_id": "y", "content": [{"type": "text", "text": "[연속 단건 조회] 거부"}]},
        ]),
        line("assistant", "2026-11-08T01:00:05Z", [
            use("Agent", model="fable", prompt="검토"),
            use("Agent", subagent_type="lookup", run_in_background="false"),
            use("Task", subagent_type="analyst", run_in_background="true"),
        ], "m3"),
        line("assistant", "2026-11-08T01:00:06Z", [use("Bash", command="ls")], "s1", isSidechain=True),
        line("assistant", "2026-11-08T01:00:07Z", [{"type": "text", "text": "끝"}], "m4"),
    ])


def test_responses_group_by_message_id(tmp_path: Path) -> None:
    session = replay.parse_session(small_session(tmp_path))
    assert session is not None
    assert [len(r) for r in session.responses] == [2, 1, 3]
    assert session.calls == 6
    assert session.multi == 2


def test_advisor_worker_and_background_rules(tmp_path: Path) -> None:
    session = replay.parse_session(small_session(tmp_path))
    assert session is not None
    assert session.advisors == 1
    assert session.workers == 2
    assert session.multi_worker_responses == 1
    # the string "false" is not background, the string "true" is
    assert session.background_workers == 1
    assert replay.is_true(True) and replay.is_true("true")
    assert not replay.is_true("false") and not replay.is_true("True") and not replay.is_true(1)


def test_markers_denials_and_sidechain(tmp_path: Path) -> None:
    session = replay.parse_session(small_session(tmp_path))
    assert session is not None
    # the same text block written on two lines of one message counts once
    assert session.plan_marks == 1
    assert session.plan_update_marks == 0
    assert session.denials == 1
    # the sidechain line is not a response of this session
    assert len(session.responses) == 3


def test_heavy_flag_and_aggregate(tmp_path: Path) -> None:
    lines = [line("user", "2026-11-08T01:00:00Z", "일을 해")]
    for i in range(replay.HEAVY):
        lines.append(line("assistant", f"2026-11-08T01:{i:02d}:01Z", [use("Bash", command="ls")], f"h{i}"))
    heavy = replay.parse_session(write(tmp_path / "bbbbbbbb-2.jsonl", lines))
    light = replay.parse_session(small_session(tmp_path))
    assert heavy is not None and light is not None
    assert heavy.heavy and not light.heavy
    total = replay.aggregate([heavy, light])
    assert total["heavy_sessions"] == 1
    assert total["heavy_with_worker"] == 0
    assert total["tool_responses"] == replay.HEAVY + 3


def test_since_filter_and_manual_table(tmp_path: Path) -> None:
    small_session(tmp_path)
    pattern = str(tmp_path / "*.jsonl")
    assert len(replay.load(pattern, replay.dt.datetime(2026, 11, 8, tzinfo=replay.KST).timestamp())) == 1
    assert replay.load(pattern, replay.dt.datetime(2026, 11, 9, tzinfo=replay.KST).timestamp()) == []
    rows = [{"session": "aaaaaaaa-1", "started": "11-08 10:00", "prompt": "p", "responses": 3, "calls": 6,
             "workers": 2, "label": "갈래형"}]
    assert "| 갈래형 | 갈래형 | O |" in "\n".join(replay.table(rows, {"aaaa": "갈래형"}))


def test_human_request_and_classify(tmp_path: Path) -> None:
    wrapped = (
        "[이 요청의 값]\n설명\n[09:00:00 김태일 <@U1>] 앞 말\n"
        "===== 여기까지가 지난 대화다 =====\n[10:00:00 김태일 <@U1>] 진짜 요청"
    )
    assert replay.human_request(wrapped) == "[10:00:00 김태일 <@U1>] 진짜 요청"
    session = replay.parse_session(small_session(tmp_path))
    assert session is not None
    # fewer than four responses is a short answer whatever the prompt says
    assert replay.classify(session)[0] == "단답형"
