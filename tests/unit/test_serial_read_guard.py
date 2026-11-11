"""guard/pretool/serial_read_guard.py 시험.

다른 가드 시험(test_pretool_guards.py)과 같이 실제 서브프로세스로 돌려 훅
프로토콜(stdin, exit 0, stdout JSON)을 함께 본다. 판정 규칙 자체는 judge() 를
직접 불러 가볍게 본다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from slack_cli_agent.engine.claude import guard_hook_settings
from slack_cli_agent.guard.pretool import serial_read_guard as srg

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"


def _assistant(message_id: str, *uses: tuple[str, str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "id": message_id,
            "content": [{"type": "tool_use", "id": i, "name": n, "input": inp} for i, n, inp in uses],
        },
    }


def _result(tool_use_id: str, text: str = "ok") -> dict[str, Any]:
    return {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": text}]},
    }


def _human(text: str = "조사해 줘") -> dict[str, Any]:
    return {"type": "user", "message": {"content": text}}


def _singles(n: int, start: int = 0) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for i in range(start, start + n):
        records += [_assistant(f"m{i}", (f"t{i}", "Read", {"file_path": f"/f{i}"})), _result(f"t{i}")]
    return records


def _write(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")


def run_guard(
    path: Path, tool_use_id: str, name: str, tool_input: dict[str, Any], env: dict[str, str] | None = None
) -> tuple[dict[str, Any] | None, subprocess.CompletedProcess[str]]:
    full_env = dict(os.environ)
    full_env["PYTHONPATH"] = str(SRC_ROOT) + os.pathsep + full_env.get("PYTHONPATH", "")
    for key in ("SERIAL_READ_RUN", "SERIAL_READ_SIBLING_WAIT_SEC"):
        full_env.pop(key, None)
    full_env["SERIAL_READ_SIBLING_WAIT_SEC"] = "0"
    # 시험은 N=4 로 고정해 기록을 짧게 둔다. 기본값은 TestJudge 에서 본다.
    full_env["SERIAL_READ_RUN"] = "4"
    if env:
        full_env.update(env)
    payload = {
        "hook_event_name": "PreToolUse", "transcript_path": str(path),
        "tool_use_id": tool_use_id, "tool_name": name, "tool_input": tool_input,
    }
    proc = subprocess.run(
        [sys.executable, str(srg.HOOK_SCRIPT)], input=json.dumps(payload, ensure_ascii=False),
        capture_output=True, text=True, env=full_env, timeout=15,
    )
    out = proc.stdout.strip()
    return (json.loads(out) if out else None), proc


class TestSerialReadGuardHook:
    def test_단건_조회_4번_뒤_조회는_거부한다(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        _write(path, [_human(), *_singles(4), _assistant("m4", ("t4", "Grep", {"pattern": "x"}))])
        result, proc = run_guard(path, "t4", "Grep", {"pattern": "x"})
        assert proc.returncode == 0
        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert out["permissionDecisionReason"].startswith(srg.MARKER)
        assert "한 응답에 함께 불러라" in out["permissionDecisionReason"]
        assert "같은 호출을 그대로 다시 불러라" in out["permissionDecisionReason"]

    def test_현재_줄이_아직_기록되지_않아도_거부한다(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        _write(path, [_human(), *_singles(4)])
        result, _ = run_guard(path, "t4", "Read", {"file_path": "/z"})
        assert result is not None

    def test_단건_조회_3번이면_통과한다(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        _write(path, [_human(), *_singles(3), _assistant("m3", ("t3", "Read", {"file_path": "/z"}))])
        result, proc = run_guard(path, "t3", "Read", {"file_path": "/z"})
        assert proc.returncode == 0
        assert result is None

    def test_쓰기_도구는_막지_않는다(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        _write(path, [_human(), *_singles(6)])
        result, _ = run_guard(path, "tx", "Edit", {"file_path": "/a"})
        assert result is None
        result, _ = run_guard(path, "tx", "Bash", {"command": "git push origin main"})
        assert result is None

    def test_거부된_호출을_그대로_다시_부르면_통과한다(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        records = [
            _human(), *_singles(4),
            _assistant("m4", ("t4", "Grep", {"pattern": "x"})), _result("t4", srg.REASON.format(run=4)),
            _assistant("m5", ("t5", "Grep", {"pattern": "x"})),
        ]
        _write(path, records)
        result, _ = run_guard(path, "t5", "Grep", {"pattern": "x"})
        assert result is None

    def test_전사의_훅_거부_접두가_붙어도_재호출을_통과시킨다(self, tmp_path: Path) -> None:
        # claude 2.1.321 은 거부를 "PreToolUse:<도구> hook error: <사유>" 로 남긴다 (2026-11-10 W5).
        path = tmp_path / "t.jsonl"
        records = [
            _human(), *_singles(4),
            _assistant("m4", ("t4", "Grep", {"pattern": "x"})),
            _result("t4", "PreToolUse:Grep hook error: " + srg.REASON.format(run=4)),
            _assistant("m5", ("t5", "Grep", {"pattern": "x"})),
        ]
        _write(path, records)
        result, _ = run_guard(path, "t5", "Grep", {"pattern": "x"})
        assert result is None

    def test_거부_뒤에는_다시_센다(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        records = [
            _human(), *_singles(4),
            _assistant("m4", ("t4", "Grep", {"pattern": "x"})), _result("t4", srg.REASON.format(run=4)),
            _assistant("m5", ("t5", "Grep", {"pattern": "x"})), _result("t5"),
            *_singles(2, start=6),
        ]
        _write(path, records)
        # 거부 뒤 단건 3번(재호출 포함)이면 아직 통과
        result, _ = run_guard(path, "t9", "Read", {"file_path": "/q"})
        assert result is None
        _write(path, [*records, *_singles(1, start=8)])
        # 4번째가 쌓이면 다시 거부
        result, _ = run_guard(path, "t10", "Read", {"file_path": "/q"})
        assert result is not None

    def test_같은_응답에_형제_호출이_있으면_통과한다(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        _write(path, [
            _human(), *_singles(4),
            _assistant("m4", ("t4", "Read", {"file_path": "/a"})),
            _assistant("m4", ("t5", "Read", {"file_path": "/b"})),
        ])
        result, _ = run_guard(path, "t4", "Read", {"file_path": "/a"})
        assert result is None

    def test_대기_중에_형제_호출이_기록되면_통과한다(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        records = [_human(), *_singles(4), _assistant("m4", ("t4", "Read", {"file_path": "/a"}))]
        _write(path, records)

        def late_sibling() -> None:
            time.sleep(0.4)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(_assistant("m4", ("t5", "Read", {"file_path": "/b"}))) + "\n")

        writer = threading.Thread(target=late_sibling)
        writer.start()
        result, _ = run_guard(path, "t4", "Read", {"file_path": "/a"}, {"SERIAL_READ_SIBLING_WAIT_SEC": "3"})
        writer.join()
        assert result is None

    def test_새_요청이면_센_것을_버린다(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        _write(path, [_human(), *_singles(4), _human("다음 일"), *_singles(1, start=10)])
        result, _ = run_guard(path, "t11", "Read", {"file_path": "/z"})
        assert result is None

    def test_N_을_환경변수로_바꾸고_0_이면_끈다(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        _write(path, [_human(), *_singles(4)])
        result, _ = run_guard(path, "t4", "Read", {"file_path": "/z"}, {"SERIAL_READ_RUN": "6"})
        assert result is None
        result, _ = run_guard(path, "t4", "Read", {"file_path": "/z"}, {"SERIAL_READ_RUN": "0"})
        assert result is None
        result, _ = run_guard(path, "t4", "Read", {"file_path": "/z"}, {"SERIAL_READ_RUN": "3"})
        assert result is not None

    def test_대화록이_없어도_실패하지_않는다(self, tmp_path: Path) -> None:
        result, proc = run_guard(tmp_path / "없음.jsonl", "t1", "Read", {"file_path": "/a"})
        assert proc.returncode == 0
        assert result is None


class TestJudge:
    def test_기본값은_단건_조회_6번이다(self) -> None:
        responses = [srg.Response(f"c{i}", [srg.Use(f"c{i}", "Read", {})]) for i in range(6)]
        assert srg.DEFAULT_RUN == 6
        assert srg.judge(responses[:5], "x", "Read", {}) == ("short_run", 5)
        assert srg.judge(responses, "x", "Read", {}) == ("deny", 6)

    def test_묶음_응답이_끼면_끊긴다(self) -> None:
        responses = [
            srg.Response("a", [srg.Use("1", "Read", {})]),
            srg.Response("b", [srg.Use("2", "Read", {}), srg.Use("3", "Grep", {})]),
            *[srg.Response(f"c{i}", [srg.Use(f"c{i}", "Read", {})]) for i in range(3)],
        ]
        assert srg.judge(responses, "x", "Read", {}, 4) == ("short_run", 3)

    def test_tool_use_id_가_없으면_마지막_같은_호출을_자기로_본다(self) -> None:
        responses = [srg.Response(f"c{i}", [srg.Use(f"c{i}", "Read", {"p": i})]) for i in range(5)]
        assert srg.judge(responses, None, "Read", {"p": 4}, 4) == ("deny", 4)


def test_엔진_설정에_모든_도구용_가드로_실린다() -> None:
    groups = guard_hook_settings()["hooks"]["PreToolUse"]
    star = [g for g in groups if g["matcher"] == "*"]
    assert len(star) == 1
    command = star[0]["hooks"][0]["command"]
    assert "serial_read_guard.py" in command
    assert command.startswith(sys.executable)
