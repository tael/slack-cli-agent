"""엔진 안에서 모델 하나의 일시 장애를 가려 폴백 모델로 재시도한다 (B9/B17).

Codex 는 2026-10-19 에 gpt-5.6-sol 이 "Selected model is at capacity" 와 429 를
돌려줘 봇 전체가 멈췄다. 계정 사용량은 2% 였고 다른 모델은 멀쩡했다. 모델 하나의
장애로 봇이 서지 않도록, 같은 Codex 엔진 안에서 폴백 모델로 같은 턴을 순차
재시도한다. 그래도 실패해야 대체 엔진 전환으로 넘어간다.

- B9: CodexEngine.detect_model_outage — 계정 벽(크레딧·한도)은 제외하고
  모델 하나의 일시 장애만 True 로 가린다.
- B17: EngineRunner.run — 모델 장애면 spec.model_fallbacks 순서로 다음 모델을
  다시 돌린다. 도구가 이미 돈 턴, 남은 시간 부족, 계정 벽은 재시도하지 않는다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from test_engine import SETTINGS, codex_profile, profile_with, request, 통과정책

from slack_cli_agent.config.profile import Profile
from slack_cli_agent.engine.codex import CodexEngine
from slack_cli_agent.engine.runner import EngineRunner


def _ok_jsonl(text: str = "성공") -> str:
    return "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "th-ok"}),
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message", "text": text}}),
        json.dumps({"type": "turn.completed",
                    "usage": {"input_tokens": 1, "output_tokens": 1}}),
    ])


class FakeCompleted:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _model_in(cmd: list[str]) -> str:
    return cmd[cmd.index("-m") + 1]


def fallback_profile(tmp_path: Path, fallbacks: list[str]) -> Profile:
    """Codex 를 1차로, model_fallbacks 를 지정한 프로필."""
    primary = {
        "type": "codex", "binary": "codex", "model": "gpt-5.6-sol",
        "model_fallbacks": fallbacks,
    }
    return profile_with(primary, tmp_path=tmp_path)


# -- B9: 모델 장애 감지


class TestDetectModelOutage:
    def test_capacity_문구는_모델_장애다(self, tmp_path: Path) -> None:
        engine = CodexEngine(codex_profile(tmp_path), SETTINGS)
        assert engine.detect_model_outage("Selected model is at capacity", "") is True

    def test_429는_모델_장애다(self, tmp_path: Path) -> None:
        engine = CodexEngine(codex_profile(tmp_path), SETTINGS)
        assert engine.detect_model_outage("", "429 Too Many Requests") is True

    def test_계정_크레딧_소진은_모델_장애가_아니다(self, tmp_path: Path) -> None:
        # 계정의 벽이라 모델을 바꿔도 같다. 429 가 섞여 있어도 폴백하지 않는다.
        engine = CodexEngine(codex_profile(tmp_path), SETTINGS)
        text = "429 too many requests\nworkspace is out of credits"
        assert engine.detect_model_outage(text, "") is False

    def test_사용_한도도_모델_장애가_아니다(self, tmp_path: Path) -> None:
        engine = CodexEngine(codex_profile(tmp_path), SETTINGS)
        assert engine.detect_model_outage("you have hit your usage limit", "") is False

    def test_무관한_실패는_모델_장애가_아니다(self, tmp_path: Path) -> None:
        engine = CodexEngine(codex_profile(tmp_path), SETTINGS)
        assert engine.detect_model_outage("some other error", "") is False

    def test_도구가_돈_턴을_가린다(self, tmp_path: Path) -> None:
        engine = CodexEngine(codex_profile(tmp_path), SETTINGS)
        assert engine.tools_already_ran('... "command_execution" ...') is True
        assert engine.tools_already_ran("아무 일도 없었다") is False


# -- B17: 폴백 모델 순차 재시도


class TestModelFallbackLoop:
    def _runner(self, fake: Any) -> EngineRunner:
        return EngineRunner(SETTINGS, subprocess_runner=fake, environment_policy=통과정책())

    def test_1차_모델_장애면_2차_모델로_재시도한다(self, tmp_path: Path) -> None:
        profile = fallback_profile(tmp_path, ["gpt-5.6-terra"])
        engine = CodexEngine(profile, SETTINGS)
        tried: list[str] = []

        def fake(cmd, cwd, timeout, **kwargs):
            model = _model_in(cmd)
            tried.append(model)
            if model == "gpt-5.6-sol":
                return FakeCompleted("Selected model is at capacity", "", 1)
            return FakeCompleted(_ok_jsonl("폴백 답변"), "", 0)

        resp = self._runner(fake).run(engine, request(model=None))

        assert tried == ["gpt-5.6-sol", "gpt-5.6-terra"]
        assert resp.ok is True
        assert resp.body == "폴백 답변"
        assert resp.model_asked == "gpt-5.6-terra"

    def test_도구가_돈_턴은_폴백하지_않는다(self, tmp_path: Path) -> None:
        # 다시 돌리면 같은 쓰기가 두 번 난다. 첫 모델 실패를 그대로 돌려준다.
        profile = fallback_profile(tmp_path, ["gpt-5.6-terra"])
        engine = CodexEngine(profile, SETTINGS)
        tried: list[str] = []

        def fake(cmd, cwd, timeout, **kwargs):
            tried.append(_model_in(cmd))
            out = 'Selected model is at capacity\n{"type":"item.started","item":{"type":"command_execution"}}'
            return FakeCompleted(out, "", 1)

        resp = self._runner(fake).run(engine, request(model=None))

        assert tried == ["gpt-5.6-sol"]
        assert resp.ok is False

    def test_계정_벽은_폴백하지_않는다(self, tmp_path: Path) -> None:
        profile = fallback_profile(tmp_path, ["gpt-5.6-terra"])
        engine = CodexEngine(profile, SETTINGS)
        tried: list[str] = []

        def fake(cmd, cwd, timeout, **kwargs):
            tried.append(_model_in(cmd))
            return FakeCompleted("workspace is out of credits", "", 1)

        resp = self._runner(fake).run(engine, request(model=None))

        assert tried == ["gpt-5.6-sol"]
        assert resp.ok is False

    def test_모델_장애_아닌_실패는_폴백하지_않는다(self, tmp_path: Path) -> None:
        profile = fallback_profile(tmp_path, ["gpt-5.6-terra"])
        engine = CodexEngine(profile, SETTINGS)
        tried: list[str] = []

        def fake(cmd, cwd, timeout, **kwargs):
            tried.append(_model_in(cmd))
            return FakeCompleted("", "그냥 실패", 1)

        resp = self._runner(fake).run(engine, request(model=None))

        assert tried == ["gpt-5.6-sol"]
        assert resp.ok is False

    def test_폴백_미설정이면_한_번만_돈다(self, tmp_path: Path) -> None:
        # 기본(폴백 목록 없음)은 재시도 없이 예전 그대로 한 번만 실행한다.
        profile = codex_profile(tmp_path)  # codex 는 fallback_engine, model_fallbacks 없음
        engine = CodexEngine(profile, SETTINGS)
        tried: list[str] = []

        def fake(cmd, cwd, timeout, **kwargs):
            tried.append(_model_in(cmd))
            return FakeCompleted("Selected model is at capacity", "", 1)

        resp = self._runner(fake).run(engine, request(model="gpt-5.6-sol"))

        assert tried == ["gpt-5.6-sol"]
        assert resp.ok is False

    def test_모든_모델_장애면_마지막_실패를_돌려준다(self, tmp_path: Path) -> None:
        profile = fallback_profile(tmp_path, ["gpt-5.6-terra"])
        engine = CodexEngine(profile, SETTINGS)
        tried: list[str] = []

        def fake(cmd, cwd, timeout, **kwargs):
            tried.append(_model_in(cmd))
            return FakeCompleted("Selected model is at capacity", "", 1)

        resp = self._runner(fake).run(engine, request(model=None))

        assert tried == ["gpt-5.6-sol", "gpt-5.6-terra"]
        assert resp.ok is False
        assert resp.model_asked == "gpt-5.6-terra"
