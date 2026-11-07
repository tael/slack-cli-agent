"""0 이 아닌 종료코드로 끝난 엔진 실행의 원문 앞부분을 보존한다 (C8).

종료코드만 남고 stdout·stderr 원문은 버려져, CLI 가 무슨 이유로 실패했는지
다시 볼 수 없었다. 마메치(bot.py)는 stderr 앞 1000자와 stdout 앞 2000자를
로그로 남겼다. 원문은 로그-세이프 경계를 지켜 FailureDetail 에 넣지 않고
앞 2KB 를 로그로만 남긴다. 엔진 공통이라 runner 한 곳에서 처리한다.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest
from test_engine import SETTINGS, RecordingEngine, claude_profile, request, 통과정책

from slack_cli_agent.engine.runner import HEAD_CHARS, EngineRunner, head_chars


def _runner(stdout: str, stderr: str, returncode: int) -> EngineRunner:
    def fake(cmd, cwd, timeout, env=None, **_):
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    return EngineRunner(SETTINGS, subprocess_runner=fake, environment_policy=통과정책())


class TestHeadChars:
    def test_폭은_2KB(self) -> None:
        assert HEAD_CHARS == 2000

    def test_길면_앞만(self) -> None:
        text = "a" * HEAD_CHARS + "TAIL"
        assert head_chars(text) == "a" * HEAD_CHARS

    def test_빈값은_빈값(self) -> None:
        assert head_chars("") == ""
        assert head_chars(None) == ""


class TestRunnerLogsNonzeroHead:
    def test_앞_2KB_를_로그로_남긴다(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        engine = RecordingEngine(claude_profile(tmp_path), SETTINGS)
        out = "OUTHEAD" + "o" * (HEAD_CHARS + 100) + "OUTEND"
        runner = _runner(out, "ERRHEAD boom", 2)
        with caplog.at_level(logging.WARNING):
            resp = runner.run(engine, request())

        assert resp.ok is False
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "종료코드 2" in joined
        assert "OUTHEAD" in joined
        assert "ERRHEAD boom" in joined
        # 앞부분만 싣는다.
        assert "OUTEND" not in joined

    def test_토큰은_가린다(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        engine = RecordingEngine(claude_profile(tmp_path), SETTINGS)
        runner = _runner("", "auth xoxb-123-abc failed", 1)
        with caplog.at_level(logging.WARNING):
            runner.run(engine, request())
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "xoxb-123-abc" not in joined
        assert "auth *** failed" in joined

    def test_성공이면_남기지_않는다(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        engine = RecordingEngine(claude_profile(tmp_path), SETTINGS)
        runner = _runner("SECRETBODY", "", 0)
        with caplog.at_level(logging.WARNING):
            resp = runner.run(engine, request())
        assert resp.ok is True
        assert "SECRETBODY" not in " ".join(r.getMessage() for r in caplog.records)

