"""시간초과로 중단한 엔진의 마지막 출력을 보존한다 (B11).

타임아웃이 나면 그때까지 엔진이 뱉은 stdout/stderr 가 통째로 버려졌다.
무엇을 하다 멈췄는지가 사라져 원인을 못 찾는다. 두 군데를 고친다.

- stream.py run_streaming: 중단 시 모아 둔 출력을 TimeoutExpired 에 실어 올린다.
- runner.py: 중단 응답에 그 출력의 꼬리 800자를 로그로 남기고, 분량을
  FailureDetail.stdout_chars 로 센다. 원문은 로그-세이프 경계를 지켜
  FailureDetail 에 넣지 않고 로그로만 보존한다.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest
from test_engine import SETTINGS, RecordingEngine, claude_profile, request, 통과정책

from slack_cli_agent.engine.runner import TAIL_CHARS, EngineRunner, tail_chars
from slack_cli_agent.engine.stream import run_streaming


class TestTailChars:
    def test_짧으면_그대로(self) -> None:
        assert tail_chars("abc") == "abc"

    def test_길면_꼬리만(self) -> None:
        text = "x" * (TAIL_CHARS + 50)
        got = tail_chars(text)
        assert len(got) == TAIL_CHARS
        assert got == text[-TAIL_CHARS:]

    def test_빈값은_빈값(self) -> None:
        assert tail_chars("") == ""
        assert tail_chars(None) == ""


class TestStreamTimeoutCarriesOutput:
    def test_중단_시_모은_출력을_예외에_싣는다(self, tmp_path: Path) -> None:
        # 마커를 먼저 찍고 오래 자는 본체. 타임아웃 전에 마커가 파이프로 나온다.
        script = "\n".join([
            "import sys, time",
            "sys.stdout.write('OUT-MARK\\n'); sys.stdout.flush()",
            "sys.stderr.write('ERR-MARK\\n'); sys.stderr.flush()",
            "time.sleep(5)",
        ])
        seen: list[str] = []
        with pytest.raises(subprocess.TimeoutExpired) as excinfo:
            run_streaming(
                [sys.executable, "-c", script],
                cwd=str(tmp_path),
                timeout=0.6,
                env=None,
                on_stdout_line=seen.append,
            )
        assert "OUT-MARK" in (excinfo.value.stdout or "")
        assert "ERR-MARK" in (excinfo.value.stderr or "")


class TestRunnerTimeoutPreservesTail:
    def _runner_with(self, fake) -> EngineRunner:
        return EngineRunner(
            SETTINGS, subprocess_runner=fake, environment_policy=통과정책()
        )

    def test_꼬리_출력을_로그로_남기고_분량을_센다(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        profile = claude_profile(tmp_path)
        engine = RecordingEngine(profile, SETTINGS)
        payload = "G" * (TAIL_CHARS + 200)

        def fake(cmd, cwd, timeout, env=None):
            raise subprocess.TimeoutExpired(
                cmd=cmd, timeout=timeout, output=payload, stderr="ERRTAIL"
            )

        runner = self._runner_with(fake)
        with caplog.at_level(logging.WARNING):
            resp = runner.run(engine, request())

        assert resp.ok is False
        assert resp.failure_reason == "timeout"
        # 분량은 로그-세이프 FailureDetail 에 수로 남는다.
        assert resp.failure_detail.stdout_chars == len(payload)
        # 원문 꼬리는 로그로만 보존된다.
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "ERRTAIL" in joined
        assert payload[-TAIL_CHARS:] in joined
        # 전체 원문이 통째로 로그에 실리진 않는다(꼬리만).
        assert payload not in joined

    def test_출력이_없으면_조용히_넘어간다(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        profile = claude_profile(tmp_path)
        engine = RecordingEngine(profile, SETTINGS)

        def fake(cmd, cwd, timeout, env=None):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

        runner = self._runner_with(fake)
        with caplog.at_level(logging.WARNING):
            resp = runner.run(engine, request())

        assert resp.failure_reason == "timeout"
        assert resp.failure_detail.stdout_chars is None
