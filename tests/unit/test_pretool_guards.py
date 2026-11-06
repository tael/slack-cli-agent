"""B4 — PreToolUse Bash 가드(turn_budget/install_guard/scan_guard)와
Stop 종료검수 훅(finish_hook), 그리고 engine/claude.py 배선 시험.

가드 스크립트는 progress_hook.py 와 같은 설계다: 표준라이브러리만 쓰는
standalone 스크립트라 엔진의 최소 허용 환경에서도 그대로 돈다. 그래서 여기서도
stdin 에 hook JSON 을 흘려 실제 서브프로세스로 돌려 보고 stdout JSON 과
exit 0 을 확인한다 (observability/progress_hook.py 를 쓰는
test_progress_delivery.py 와 달리 이 모듈들은 단순 함수 호출로 단위시험하기보다
훅 프로토콜 전체 - stdin 읽기, exit 코드, stdout 형식 - 를 같이 확인하는 쪽이
실수가 적다).

finish_hook.py 만 예외로 slack_cli_agent.guard.finish_review 를 import 하므로
(모듈 docstring 참고), 서브프로세스에 PYTHONPATH 를 이 워크트리의 src 로 앞서
줘서 설치된 패키지가 아니라 지금 짜고 있는 코드를 import 하게 만든다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from slack_cli_agent.guard import finish_hook
from slack_cli_agent.guard.posttool import parallel_nudge
from slack_cli_agent.guard.pretool import (
    install_guard,
    restart_guard,
    scan_guard,
    turn_budget,
)

#: 이 워크트리의 src 루트. finish_hook.py 서브프로세스가 설치된(다른 워크트리의)
#: slack_cli_agent 가 아니라 지금 짜고 있는 코드를 import 하도록 앞에 둔다.
SRC_ROOT = Path(__file__).resolve().parents[2] / "src"

#: 훅이 스스로 읽을 일이 없는데 테스트 바깥 환경에 남아 있으면 판정을 오염시킬
#: 수 있는 변수들. 서브프로세스마다 지우고 테스트가 명시한 값만 준다.
_DEADLINE_VARS = (
    "TURN_DEADLINE", "TURN_DEADLINE_MONO", "TURN_WRAPUP_SEC", "TURN_BUDGET_LOG",
    "POST_CHANNEL", "ADVISOR_CHANNELS", "CODE_FIRST_CHANNELS",
    "RESTART_GUARD_LABELS", "RESTART_GUARD_MARKER",
    "SCA_FINISH_VERIFY", "SCA_FINISH_VERIFY_MODEL", "SCA_VERIFY_NESTED",
)


def run_hook(
    script: Path, payload: dict[str, Any], env: dict[str, str] | None = None
) -> tuple[dict[str, Any] | None, subprocess.CompletedProcess[str]]:
    full_env = dict(os.environ)
    full_env["PYTHONPATH"] = str(SRC_ROOT) + os.pathsep + full_env.get("PYTHONPATH", "")
    for key in _DEADLINE_VARS:
        full_env.pop(key, None)
    if env:
        full_env.update(env)
    proc = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        env=full_env,
        timeout=15,
    )
    stdout = proc.stdout.strip()
    data = json.loads(stdout) if stdout else None
    return data, proc


class TestTurnBudget:
    def test_마감_임박에_긴_timeout_은_거부한다(self) -> None:
        mono_deadline = time.monotonic() + 30  # 여유(60초)를 빼면 이미 마이너스
        payload = {
            "tool_name": "Bash",
            "session_id": "s1",
            "tool_input": {"command": "sleep 100", "timeout": 90_000},
        }
        result, proc = run_hook(
            turn_budget.HOOK_SCRIPT, payload, {"TURN_DEADLINE_MONO": str(mono_deadline)}
        )
        assert proc.returncode == 0
        assert result is not None
        hook_out = result["hookSpecificOutput"]
        assert hook_out["hookEventName"] == "PreToolUse"
        assert hook_out["permissionDecision"] == "deny"

    def test_run_in_background_은_마감이_임박해도_통과한다(self) -> None:
        mono_deadline = time.monotonic() + 5
        payload = {
            "tool_name": "Bash",
            "session_id": "s1",
            "tool_input": {"command": "sleep 100", "timeout": 90_000, "run_in_background": True},
        }
        result, proc = run_hook(
            turn_budget.HOOK_SCRIPT, payload, {"TURN_DEADLINE_MONO": str(mono_deadline)}
        )
        assert proc.returncode == 0
        assert result is None

    def test_deadline_env_이_없으면_사람_세션이라_통과한다(self) -> None:
        payload = {
            "tool_name": "Bash",
            "session_id": "s1",
            "tool_input": {"command": "sleep 900", "timeout": 900_000},
        }
        result, proc = run_hook(turn_budget.HOOK_SCRIPT, payload)
        assert proc.returncode == 0
        assert result is None

    def test_시간이_충분하면_통과한다(self) -> None:
        mono_deadline = time.monotonic() + 10_000
        payload = {
            "tool_name": "Bash",
            "session_id": "s1",
            # timeout 을 안 주면 기본 120초로 본다 - 그래도 10000초 마감엔 넉넉하다
            "tool_input": {"command": "ls"},
        }
        result, proc = run_hook(
            turn_budget.HOOK_SCRIPT, payload, {"TURN_DEADLINE_MONO": str(mono_deadline)}
        )
        assert proc.returncode == 0
        assert result is None


class TestRestartGuard:
    def _run(
        self, command: str, env: dict[str, str] | None = None
    ) -> tuple[dict[str, Any] | None, subprocess.CompletedProcess[str]]:
        payload = {"tool_name": "Bash", "session_id": "s1", "tool_input": {"command": command}}
        return run_hook(restart_guard.HOOK_SCRIPT, payload, env)

    def test_launchctl_kickstart_은_거부한다(self) -> None:
        result, proc = self._run("launchctl kickstart -k gui/501/local.guchipachi.worker")
        assert proc.returncode == 0
        assert result is not None
        hook_out = result["hookSpecificOutput"]
        assert hook_out["hookEventName"] == "PreToolUse"
        assert hook_out["permissionDecision"] == "deny"

    def test_launchctl_list_조회는_통과한다(self) -> None:
        result, proc = self._run("launchctl list | grep guchipachi")
        assert proc.returncode == 0
        assert result is None

    def test_재기동_스크립트는_거부한다(self) -> None:
        result, proc = self._run("~/.guchipachi-sca/apply-restart.sh")
        assert proc.returncode == 0
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_마커경로가_있으면_사유에_담긴다(self) -> None:
        result, proc = self._run(
            "launchctl stop local.guchipachi.worker",
            {"RESTART_GUARD_MARKER": "/tmp/apply-request"},
        )
        assert proc.returncode == 0
        assert result is not None
        assert "/tmp/apply-request" in result["hookSpecificOutput"]["permissionDecisionReason"]

    def test_봇라벨을_겨냥한_kill_은_거부한다(self) -> None:
        result, proc = self._run(
            "pkill -f local.guchipachi.worker", {"RESTART_GUARD_LABELS": "local.guchipachi.worker"}
        )
        assert proc.returncode == 0
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_무관한_프로세스_kill_은_통과한다(self) -> None:
        result, proc = self._run(
            "pkill -f some_unrelated_job", {"RESTART_GUARD_LABELS": "local.guchipachi.worker"}
        )
        assert proc.returncode == 0
        assert result is None

    def test_라벨이_없으면_일반_kill_은_통과한다(self) -> None:
        result, proc = self._run("kill 12345")
        assert proc.returncode == 0
        assert result is None

    def test_재기동과_무관한_명령은_통과한다(self) -> None:
        result, proc = self._run("echo restart please")
        assert proc.returncode == 0
        assert result is None


class TestInstallGuard:
    def _env(self, tmp_path: Path) -> dict[str, str]:
        return {
            "INSTALL_GUARD_MARK_DIR": str(tmp_path / "mark"),
            "INSTALL_GUARD_RESULT_DIR": str(tmp_path / "result"),
        }

    def test_미확인_설치명령은_거부한다(self, tmp_path: Path) -> None:
        payload = {
            "tool_name": "Bash",
            "session_id": "sess-1",
            "tool_input": {"command": "brew install ghidra"},
        }
        result, proc = run_hook(install_guard.HOOK_SCRIPT, payload, self._env(tmp_path))
        assert proc.returncode == 0
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_확인_이력이_있으면_통과한다(self, tmp_path: Path) -> None:
        env = self._env(tmp_path)
        check_payload = {
            "tool_name": "Bash",
            "session_id": "sess-2",
            "tool_input": {"command": "installed-check ghidra"},
        }
        checked, proc1 = run_hook(install_guard.HOOK_SCRIPT, check_payload, env)
        assert proc1.returncode == 0
        assert checked is None

        install_payload = {
            "tool_name": "Bash",
            "session_id": "sess-2",
            "tool_input": {"command": "brew install ghidra"},
        }
        result, proc2 = run_hook(install_guard.HOOK_SCRIPT, install_payload, env)
        assert proc2.returncode == 0
        assert result is None

    def test_설치_명령이_아니면_통과한다(self, tmp_path: Path) -> None:
        payload = {
            "tool_name": "Bash",
            "session_id": "sess-3",
            "tool_input": {"command": "ls -la"},
        }
        result, proc = run_hook(install_guard.HOOK_SCRIPT, payload, self._env(tmp_path))
        assert proc.returncode == 0
        assert result is None


class TestScanGuard:
    def test_find_루트_전체_스캔은_prune_재작성되고_로그가_한_줄_남는다(self, tmp_path: Path) -> None:
        log_path = tmp_path / "scan_guard.jsonl"
        payload = {
            "tool_name": "Bash",
            "session_id": "sg-1",
            "cwd": "/tmp",
            "tool_input": {"command": "find / -iname 'channels.json'"},
        }
        result, proc = run_hook(
            scan_guard.HOOK_SCRIPT, payload, {"SCAN_GUARD_LOG": str(log_path)}
        )
        assert proc.returncode == 0
        assert result is not None
        hook_out = result["hookSpecificOutput"]
        # 거부가 아니라 -prune 재작성으로 그대로 실행되는 경로다
        denied = hook_out.get("permissionDecision") == "deny"
        rewritten = "updatedInput" in hook_out
        assert denied or rewritten
        if rewritten:
            assert "/" in hook_out["updatedInput"]["command"] or "-prune" in hook_out["updatedInput"]["command"]
        assert log_path.exists()
        assert len(log_path.read_text(encoding="utf-8").strip().splitlines()) == 1

    def test_보호경로가_없는_find는_통과한다(self, tmp_path: Path) -> None:
        log_path = tmp_path / "scan_guard.jsonl"
        payload = {
            "tool_name": "Bash",
            "session_id": "sg-2",
            "cwd": "/tmp",
            "tool_input": {"command": "find /tmp -maxdepth 2 -name 'x.txt'"},
        }
        result, proc = run_hook(
            scan_guard.HOOK_SCRIPT, payload, {"SCAN_GUARD_LOG": str(log_path)}
        )
        assert proc.returncode == 0
        assert result is None
        assert not log_path.exists()


def _assistant(message_id: str, *uses: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "id": message_id,
            "content": [{"type": "tool_use", "name": n, "input": i} for n, i in uses],
        },
    }


def _tool_result() -> dict[str, Any]:
    return {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}


def _human(text: str = "조사해 줘") -> dict[str, Any]:
    return {"type": "user", "message": {"content": text}}


class TestParallelNudge:
    def _payload(self, tmp_path: Path, records: list[dict[str, Any]]) -> dict[str, Any]:
        path = tmp_path / "transcript.jsonl"
        path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8",
        )
        return {"hook_event_name": "PostToolUse", "transcript_path": str(path)}

    def _single_reads(self, n: int) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = [_human()]
        for i in range(n):
            records += [_assistant(f"m{i}", ("Read", {"file_path": f"/f{i}"})), _tool_result()]
        return records

    def test_단일_조회가_세번_이어지면_알린다(self, tmp_path: Path) -> None:
        result, proc = run_hook(parallel_nudge.HOOK_SCRIPT, self._payload(tmp_path, self._single_reads(3)))
        assert proc.returncode == 0
        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["hookEventName"] == "PostToolUse"
        assert "3번" in out["additionalContext"]
        assert "permissionDecision" not in out

    def test_두번이면_조용하다(self, tmp_path: Path) -> None:
        result, proc = run_hook(parallel_nudge.HOOK_SCRIPT, self._payload(tmp_path, self._single_reads(2)))
        assert proc.returncode == 0
        assert result is None

    def test_묶어_부른_응답이_끼면_다시_센다(self, tmp_path: Path) -> None:
        records = [
            *self._single_reads(2),
            _assistant("mb", ("Read", {"file_path": "/a"}), ("Grep", {"pattern": "x"})), _tool_result(),
            _assistant("mc", ("Glob", {"pattern": "*"})), _tool_result(),
        ]
        result, _ = run_hook(parallel_nudge.HOOK_SCRIPT, self._payload(tmp_path, records))
        assert result is None

    def test_새_요청이_오면_센_것을_버린다(self, tmp_path: Path) -> None:
        records = [
            *self._single_reads(2), _human("다음 일"),
            _assistant("n1", ("Read", {"file_path": "/x"})), _tool_result(),
        ]
        result, _ = run_hook(parallel_nudge.HOOK_SCRIPT, self._payload(tmp_path, records))
        assert result is None

    def test_쓰기_명령은_조회로_세지_않는다(self) -> None:
        assert parallel_nudge.is_read("Bash", {"command": "git push origin main"}) is False
        assert parallel_nudge.is_read("Bash", {"command": "rg foo src"}) is True
        assert parallel_nudge.is_read("mcp__slack__slack_search", {}) is True
        assert parallel_nudge.is_read("mcp__slack__slack_post_message", {}) is False
        assert parallel_nudge.is_read("Edit", {}) is False

    def test_대화록이_없어도_실패하지_않는다(self, tmp_path: Path) -> None:
        result, proc = run_hook(
            parallel_nudge.HOOK_SCRIPT, {"transcript_path": str(tmp_path / "없음.jsonl")},
        )
        assert proc.returncode == 0
        assert result is None


class TestFinishHook:
    def test_transcript_가_없으면_빈_출력으로_통과한다(self, tmp_path: Path) -> None:
        payload = {
            "session_id": "fh-1",
            "transcript_path": str(tmp_path / "없는파일.jsonl"),
        }
        result, proc = run_hook(finish_hook.HOOK_SCRIPT, payload)
        assert proc.returncode == 0
        assert proc.stdout.strip() == ""
        assert result is None

    def test_위반이_있으면_두_번은_막고_세_번째는_풀어준다(self, tmp_path: Path) -> None:
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text(
            json.dumps({
                "type": "user",
                "uuid": "turn-1",
                "message": {"role": "user", "content": "확인해줘"},
            }, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        env = {
            "FINISH_HOOK_STATE_DIR": str(tmp_path / "state"),
            "TURN_DEADLINE_MONO": str(time.monotonic() + 10_000),
        }
        payload = {
            "session_id": "fh-2",
            "transcript_path": str(transcript_path),
            "last_assistant_message": (
                "지금까지 로그 두 건을 봤다.\n원하시면 나머지도 이어서 확인해 볼게요"
            ),
        }

        first, proc1 = run_hook(finish_hook.HOOK_SCRIPT, payload, env)
        assert proc1.returncode == 0
        assert first is not None and first["decision"] == "block" and first["reason"]

        second, proc2 = run_hook(finish_hook.HOOK_SCRIPT, payload, env)
        assert proc2.returncode == 0
        assert second is not None and second["decision"] == "block"

        third, proc3 = run_hook(finish_hook.HOOK_SCRIPT, payload, env)
        assert proc3.returncode == 0
        assert proc3.stdout.strip() == ""
        assert third is None

    def _advisor_payload(self, tmp_path: Path) -> dict[str, Any]:
        transcript_path = tmp_path / "advisor.jsonl"
        transcript_path.write_text(
            json.dumps({
                "type": "user",
                "uuid": "adv-turn",
                "message": {"role": "user", "content": "A안 B안 중 뭐로 갈까"},
            }, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return {
            "session_id": "fh-adv",
            "transcript_path": str(transcript_path),
            "last_assistant_message": "### 방안\n1. A안\n2. B안\n[[ASK]]",
        }

    def test_자문채널_결정형답에_조언자_미호출이면_막는다(self, tmp_path: Path) -> None:
        """G16 배선: POST_CHANNEL 이 ADVISOR_CHANNELS 에 들면 종료검수가
        advisor_skipped 로 차단한다."""
        env = {
            "FINISH_HOOK_STATE_DIR": str(tmp_path / "state"),
            "TURN_DEADLINE_MONO": str(time.monotonic() + 10_000),
            "POST_CHANNEL": "C_ADVISOR",
            "ADVISOR_CHANNELS": "C_ADVISOR,C_OTHER",
        }
        result, proc = run_hook(finish_hook.HOOK_SCRIPT, self._advisor_payload(tmp_path), env)
        assert proc.returncode == 0
        assert result is not None and result["decision"] == "block"

    def test_비자문채널이면_같은_결정형답도_통과한다(self, tmp_path: Path) -> None:
        env = {
            "FINISH_HOOK_STATE_DIR": str(tmp_path / "state"),
            "TURN_DEADLINE_MONO": str(time.monotonic() + 10_000),
            "POST_CHANNEL": "C_PLAIN",
            "ADVISOR_CHANNELS": "C_ADVISOR,C_OTHER",
        }
        result, proc = run_hook(finish_hook.HOOK_SCRIPT, self._advisor_payload(tmp_path), env)
        assert proc.returncode == 0
        assert proc.stdout.strip() == ""
        assert result is None

    def _code_first_payload(self, tmp_path: Path) -> dict[str, Any]:
        transcript_path = tmp_path / "codefirst.jsonl"
        transcript_path.write_text(
            json.dumps({
                "type": "user",
                "uuid": "cf-turn",
                "message": {"role": "user", "content": "cm_v1 도 재무리포트로 분류하지?"},
            }, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return {
            "session_id": "fh-cf",
            "transcript_path": str(transcript_path),
            "last_assistant_message": "트래커 기준으로 cm_v1 은 재무리포트예요. https://notion.so/abc",
        }

    def test_코드우선채널_문서만_근거면_막는다(self, tmp_path: Path) -> None:
        """G13 배선: POST_CHANNEL 이 CODE_FIRST_CHANNELS 에 들면 종료검수가
        code_first 로 차단한다."""
        env = {
            "FINISH_HOOK_STATE_DIR": str(tmp_path / "state"),
            "TURN_DEADLINE_MONO": str(time.monotonic() + 10_000),
            "POST_CHANNEL": "C_CODE",
            "CODE_FIRST_CHANNELS": "C_CODE,C_OTHER",
        }
        result, proc = run_hook(finish_hook.HOOK_SCRIPT, self._code_first_payload(tmp_path), env)
        assert proc.returncode == 0
        assert result is not None and result["decision"] == "block"

    def test_비코드우선채널이면_같은_문서답도_통과한다(self, tmp_path: Path) -> None:
        env = {
            "FINISH_HOOK_STATE_DIR": str(tmp_path / "state"),
            "TURN_DEADLINE_MONO": str(time.monotonic() + 10_000),
            "POST_CHANNEL": "C_PLAIN",
            "CODE_FIRST_CHANNELS": "C_CODE,C_OTHER",
        }
        result, proc = run_hook(finish_hook.HOOK_SCRIPT, self._code_first_payload(tmp_path), env)
        assert proc.returncode == 0
        assert proc.stdout.strip() == ""
        assert result is None


class TestClaudeEngineWiring:
    def test_guard_hook_settings_에_bash_가드_4개와_stop_훅이_있다(self) -> None:
        from slack_cli_agent.engine.claude import guard_hook_settings

        settings = guard_hook_settings()
        pretool_groups = settings["hooks"]["PreToolUse"]
        bash_group = next(g for g in pretool_groups if g["matcher"] == "Bash")
        commands = [h["command"] for h in bash_group["hooks"]]
        assert len(commands) == 4
        assert any("turn_budget.py" in c for c in commands)
        assert any("restart_guard.py" in c for c in commands)
        assert any("install_guard.py" in c for c in commands)
        assert any("scan_guard.py" in c for c in commands)
        assert all(c.startswith(sys.executable) for c in commands)

        stop_groups = settings["hooks"]["Stop"]
        assert len(stop_groups) == 1
        assert stop_groups[0]["matcher"] == "*"
        assert "finish_hook.py" in stop_groups[0]["hooks"][0]["command"]

        posttool_groups = settings["hooks"]["PostToolUse"]
        assert len(posttool_groups) == 1
        assert posttool_groups[0]["matcher"] == "*"
        assert "parallel_nudge.py" in posttool_groups[0]["hooks"][0]["command"]

    def test_build_command_의_settings에_progress_훅과_bash_가드가_함께_실린다(
        self, tmp_path: Path
    ) -> None:
        from slack_cli_agent.auth.principal import TrustLevel
        from slack_cli_agent.config.profile import Profile
        from slack_cli_agent.config.settings import RuntimeSettings
        from slack_cli_agent.engine.base import EngineRequest
        from slack_cli_agent.engine.claude import ClaudeEngine
        from slack_cli_agent.engine.tool_selection import ToolSelection

        profile = Profile.from_dict({
            "name": "example",
            "primary_engine": {"type": "claude", "binary": "claude", "model": "claude-sonnet-5"},
            "owner_user_id": "U1",
            "troubleshoot_channel": "C1",
            "state_dir": str(tmp_path / "state"),
        })
        engine = ClaudeEngine(profile, RuntimeSettings())
        req = EngineRequest(
            prompt="안녕",
            system_prompt="시스템 지침",
            session_id="11111111-1111-1111-1111-111111111111",
            resume=False,
            model="claude-sonnet-5",
            effort="medium",
            workdir=tmp_path,
            readable_dirs=(tmp_path,),
            tools=ToolSelection.allow(["Read", "Grep"]),
            trust_level=TrustLevel.GENERAL,
            progress_log=tmp_path / "progress" / "C1-1.0.log",
        )
        cmd = engine.build_command(req)
        settings = json.loads(cmd[cmd.index("--settings") + 1])
        pretool_groups = settings["hooks"]["PreToolUse"]
        matchers = [g["matcher"] for g in pretool_groups]
        assert "*" in matchers  # progress 훅
        assert "Bash" in matchers  # 가드 3종
        bash_group = next(g for g in pretool_groups if g["matcher"] == "Bash")
        assert len(bash_group["hooks"]) == 4
        assert settings["hooks"]["Stop"][0]["matcher"] == "*"
