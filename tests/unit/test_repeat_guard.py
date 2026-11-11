"""guard/pretool/repeat_guard.py 시험.

serial_read_guard 시험과 같이 실제 서브프로세스로 돌려 훅 프로토콜(stdin,
exit 0, stdout JSON)을 함께 보고, 판정 규칙은 judge() 와 similar() 를 직접
불러 가볍게 본다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from slack_cli_agent.engine.claude import guard_hook_settings
from slack_cli_agent.guard.pretool import repeat_guard as rg
from slack_cli_agent.guard.pretool.serial_read_guard import Response, Use

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"

CMD = "uv run python q.py --day 2026-11-07"


def _assistant(message_id: str, *uses: tuple[str, str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "id": message_id,
            "content": [{"type": "tool_use", "id": i, "name": n, "input": inp} for i, n, inp in uses],
        },
    }


def _result(tool_use_id: str, text: str = "ok", is_error: bool = False) -> dict[str, Any]:
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": text}
    if is_error:
        block["is_error"] = True
    return {"type": "user", "message": {"content": [block]}}


def _human(text: str = "조사해 줘") -> dict[str, Any]:
    return {"type": "user", "message": {"content": text}}


def _bash(n: int, command: str = CMD, start: int = 0, output: str | None = None) -> list[dict[str, Any]]:
    """n responses of the same command. Each fails with a different message
    unless ``output`` is given, which every call then returns."""
    records: list[dict[str, Any]] = []
    for i in range(start, start + n):
        result = _result(f"t{i}", output) if output is not None else _result(f"t{i}", f"Exit code 1 line {i}", True)
        records += [_assistant(f"m{i}", (f"t{i}", "Bash", {"command": command})), result]
    return records


def _write(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")


def run_guard(
    path: Path, tool_use_id: str, name: str, tool_input: dict[str, Any], env: dict[str, str] | None = None,
) -> tuple[dict[str, Any] | None, subprocess.CompletedProcess[str]]:
    full_env = dict(os.environ)
    full_env["PYTHONPATH"] = str(SRC_ROOT) + os.pathsep + full_env.get("PYTHONPATH", "")
    for key in ("REPEAT_GUARD_RUN", "REPEAT_GUARD_SIMILARITY"):
        full_env.pop(key, None)
    if env:
        full_env.update(env)
    payload = {
        "hook_event_name": "PreToolUse", "transcript_path": str(path),
        "tool_use_id": tool_use_id, "tool_name": name, "tool_input": tool_input,
    }
    proc = subprocess.run(
        [sys.executable, str(rg.HOOK_SCRIPT)], input=json.dumps(payload, ensure_ascii=False),
        capture_output=True, text=True, env=full_env, timeout=15,
    )
    out = proc.stdout.strip()
    return (json.loads(out) if out else None), proc


def _denied(out: dict[str, Any] | None) -> bool:
    return bool(out) and out["hookSpecificOutput"]["permissionDecision"] == "deny"  # type: ignore[index]


class TestRepeatGuardHook:
    def test_같은_호출_3번_뒤_4번째는_거부한다(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        _write(path, [_human(), *_bash(3), _assistant("m3", ("t3", "Bash", {"command": CMD}))])
        out, proc = run_guard(path, "t3", "Bash", {"command": CMD})
        assert proc.returncode == 0
        assert _denied(out)
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]  # type: ignore[index]
        assert reason.startswith(rg.MARKER)
        assert "3번" in reason
        assert "스크립트" in reason

    def test_현재_줄이_아직_기록되지_않아도_거부한다(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        _write(path, [_human(), *_bash(3)])
        out, _ = run_guard(path, "t3", "Bash", {"command": CMD})
        assert _denied(out)

    def test_같은_출력을_3번_받은_뒤에도_거부한다(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        _write(path, [_human(), *_bash(3, output="running\n")])
        out, _ = run_guard(path, "t3", "Bash", {"command": CMD})
        assert _denied(out)

    def test_결과가_매번_달라지면_진행으로_보고_통과한다(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        records = [_human()]
        for i in range(3):
            records += [_assistant(f"m{i}", (f"t{i}", "Bash", {"command": CMD})), _result(f"t{i}", f"rows {i}")]
        _write(path, records)
        out, _ = run_guard(path, "t3", "Bash", {"command": CMD})
        assert out is None

    def test_같은_호출_2번이면_통과한다(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        _write(path, [_human(), *_bash(2)])
        out, proc = run_guard(path, "t2", "Bash", {"command": CMD})
        assert proc.returncode == 0
        assert out is None

    def test_공백과_설명만_다른_명령도_같은_호출로_본다(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        _write(path, [_human(), *_bash(3)])
        spaced = {"command": "uv  run python q.py\n--day 2026-11-07", "description": "다시"}
        out, _ = run_guard(path, "t3", "Bash", spaced)
        assert _denied(out)

    def test_다른_명령이_끼면_끊긴다(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        records = [_human(), *_bash(2), _assistant("x", ("tx", "Bash", {"command": "cat q.py"})), _result("tx"),
                   *_bash(1, start=5)]
        _write(path, records)
        out, _ = run_guard(path, "t9", "Bash", {"command": CMD})
        assert out is None

    def test_거부된_뒤_다시_부르면_통과하고_다시_센다(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        denied = rg.REASON.format(run=3)
        records = [_human(), *_bash(3),
                   _assistant("m3", ("t3", "Bash", {"command": CMD})), _result("t3", denied)]
        _write(path, records)
        out, _ = run_guard(path, "t4", "Bash", {"command": CMD})
        assert out is None
        records += _bash(3, start=4)
        _write(path, records)
        out, _ = run_guard(path, "t7", "Bash", {"command": CMD})
        assert _denied(out)

    def test_전사의_훅_거부_접두가_붙어도_자기_거부로_본다(self, tmp_path: Path) -> None:
        # claude 2.1.321 은 거부를 "PreToolUse:<도구> hook error: <사유>" 로 남긴다 (2026-11-10 W5).
        path = tmp_path / "t.jsonl"
        denied = "PreToolUse:Bash hook error: " + rg.REASON.format(run=3)
        records = [_human(), *_bash(3),
                   _assistant("m3", ("t3", "Bash", {"command": CMD})), _result("t3", denied, is_error=True)]
        _write(path, records)
        out, _ = run_guard(path, "t4", "Bash", {"command": CMD})
        assert out is None

    def test_다른_가드의_거부는_끊지_않는다(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        records = [_human(), *_bash(2),
                   _assistant("m2", ("t2", "Bash", {"command": CMD})), _result("t2", "[연속 단건 조회] 묶어라", True)]
        _write(path, records)
        out, _ = run_guard(path, "t3", "Bash", {"command": CMD})
        assert _denied(out)

    def test_새_요청이면_센_것을_버린다(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        _write(path, [_human(), *_bash(3), _human("다시 해 줘")])
        out, _ = run_guard(path, "t3", "Bash", {"command": CMD})
        assert out is None

    def test_N_을_환경변수로_바꾸고_0_이면_끈다(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        _write(path, [_human(), *_bash(2)])
        out, _ = run_guard(path, "t2", "Bash", {"command": CMD}, env={"REPEAT_GUARD_RUN": "2"})
        assert _denied(out)
        _write(path, [_human(), *_bash(5)])
        out, _ = run_guard(path, "t5", "Bash", {"command": CMD}, env={"REPEAT_GUARD_RUN": "0"})
        assert out is None

    def test_유사도를_1로_두면_정확히_같은_호출만_센다(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        _write(path, [_human(), *_bash(3)])
        near = {"command": CMD.replace("07", "08")}
        out, _ = run_guard(path, "t3", "Bash", near)
        assert _denied(out)
        out, _ = run_guard(path, "t3", "Bash", near, env={"REPEAT_GUARD_SIMILARITY": "1"})
        assert out is None

    def test_대화록이_없어도_실패하지_않는다(self, tmp_path: Path) -> None:
        out, proc = run_guard(tmp_path / "없음.jsonl", "t0", "Bash", {"command": CMD})
        assert proc.returncode == 0
        assert out is None

    def test_긴_대화록도_빨리_판정한다(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        long_cmd = "python - <<'EOF'\n" + "print(1)\n" * 400 + "EOF"
        records = [_human()]
        for i in range(200):
            use = (f"a{i}", "Bash", {"command": long_cmd + str(i)})
            records += [_assistant(f"a{i}", use), _result(f"a{i}", "x" * 500)]
        _write(path, records)
        started = time.monotonic()
        run_guard(path, "z", "Bash", {"command": long_cmd})
        assert time.monotonic() - started < 5


class TestSimilar:
    def test_도구가_다르면_다르다(self) -> None:
        assert not rg.similar("Bash", {"command": "ls"}, "Grep", {"command": "ls"})

    def test_다른_파일은_경로가_길게_겹쳐도_다르다(self) -> None:
        base = "/Users/someone/Jobs/tasks/a-very-long-task-directory-name/"
        assert not rg.similar("Read", {"file_path": base + "run1.out"}, "Read", {"file_path": base + "runner.log"})

    def test_쪽_넘김은_반복이_아니다(self) -> None:
        a = {"file_path": "/a/very/long/path/to/some/file.py", "offset": 100, "limit": 50}
        b = {**a, "offset": 150}
        assert not rg.similar("Read", a, "Read", b)
        assert rg.similar("Read", a, "Read", dict(a))

    def test_입력이_조금_다르면_유사도로_본다(self) -> None:
        sql = {"statement": "SELECT count(*) FROM orders WHERE created_at >= '2026-11-01' AND status = 'DONE'"}
        fixed = {"statement": sql["statement"].replace("DONE", "DONE2")}
        other = {"statement": "SELECT * FROM riders LIMIT 10"}
        assert rg.similar("q", sql, "q", fixed)
        assert not rg.similar("q", sql, "q", other)
        assert not rg.similar("q", sql, "q", fixed, threshold=1.0)

    def test_긴_스크립트도_한_곳만_바뀌면_같은_호출로_본다(self) -> None:
        body = "".join(f"print({i})\n" for i in range(800))
        a = {"command": f"python - <<'EOF'\n{body}EOF"}
        b = {"command": a["command"].replace("print(400)", "print(401)")}
        started = time.monotonic()
        assert rg.similar("Bash", a, "Bash", b)
        assert not rg.similar("Bash", a, "Bash", {"command": "python - <<'EOF'\nprint('x')\nEOF"})
        assert time.monotonic() - started < 0.5


class TestJudge:
    def test_기본값은_앞선_응답_3개다(self) -> None:
        assert rg.DEFAULT_RUN == 3
        responses = [Response(f"m{i}", [Use(f"t{i}", "Bash", {"command": CMD}, result="same")]) for i in range(3)]
        assert rg.judge(responses, "t3", "Bash", {"command": CMD}) == ("deny", 3)

    def test_묶음_속의_같은_호출도_센다(self) -> None:
        responses = [
            Response(f"m{i}", [
                Use(f"t{i}", "Bash", {"command": CMD}, result="same"), Use(f"u{i}", "Read", {"file_path": "/x"}),
            ])
            for i in range(3)
        ]
        assert rg.judge(responses, "t3", "Bash", {"command": CMD})[0] == "deny"

    def test_결과가_다르면_진행이다(self) -> None:
        responses = [
            Response(f"m{i}", [Use(f"t{i}", "Bash", {"command": CMD}, result=f"page {i}")]) for i in range(3)
        ]
        assert rg.judge(responses, "t3", "Bash", {"command": CMD}) == ("progress", 3)

    def test_결과가_아직_없으면_막지_않는다(self) -> None:
        responses = [Response(f"m{i}", [Use(f"t{i}", "Bash", {"command": CMD})]) for i in range(3)]
        assert rg.judge(responses, "t3", "Bash", {"command": CMD})[0] == "progress"

    def test_TodoWrite_는_막지_않는다(self) -> None:
        todo = {"todos": [{"content": "a", "status": "in_progress"}]}
        responses = [Response(f"m{i}", [Use(f"t{i}", "TodoWrite", todo)]) for i in range(5)]
        assert rg.judge(responses, "t5", "TodoWrite", todo) == ("exempt", 0)

    def test_tool_use_id_가_없으면_마지막_같은_호출을_자기로_본다(self) -> None:
        responses = [Response(f"m{i}", [Use(f"t{i}", "Bash", {"command": CMD}, result="same")]) for i in range(4)]
        assert rg.judge(responses, None, "Bash", {"command": CMD}) == ("deny", 3)


def test_엔진_설정에_모든_도구용_가드로_실린다() -> None:
    groups = guard_hook_settings()["hooks"]["PreToolUse"]
    star = [g for g in groups if g["matcher"] == "*"]
    assert len(star) == 1
    commands = [h["command"] for h in star[0]["hooks"]]
    assert any("repeat_guard.py" in c for c in commands)
    assert all(c.startswith(sys.executable) for c in commands)
