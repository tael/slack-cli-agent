"""대체 실행기 복귀 확인과 하단 모델 표기 (2026-11-13 구치파치).

- 18:48 에 codex 로 넘어간 뒤 19:51 복귀 확인이 codex 세션 ID 로 claude 를
  이어 실패했고, 그 실패를 미복구로 읽어 codex 에 머물렀다
- 하단 "실행 모델" 이 채널 설정값을 적어 codex 턴을 claude-opus-5-5 로 표기했다
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from test_engine import SETTINGS, FakeCompleted, RecordingEngine, codex_profile, named, 통과정책

from slack_cli_agent.core.pipeline import RequestPipeline
from slack_cli_agent.engine.base import EngineRequest, EngineResponse
from slack_cli_agent.engine.runner import EngineRunner, FallbackEngine
from slack_cli_agent.engine.switcher import EngineSwitcher


def _fallback(tmp_path: Path, returncodes: list[int]):
    profile = codex_profile(tmp_path)
    primary = named(RecordingEngine, "claude")(profile, SETTINGS)
    secondary = named(RecordingEngine, "codex")(profile, SETTINGS)
    switcher = EngineSwitcher(tmp_path / "engine_state.json")
    codes = iter(returncodes)
    runner = EngineRunner(
        SETTINGS,
        subprocess_runner=lambda cmd, cwd, timeout, env=None: FakeCompleted(
            stdout="답", returncode=next(codes)),
        environment_policy=통과정책(),
    )
    fallback = FallbackEngine(primary, secondary, switcher, runner)
    fallback._notify_recovery = lambda: None  # type: ignore[method-assign]
    return fallback, primary, switcher


def _request(resume: bool) -> EngineRequest:
    return EngineRequest(
        prompt="p", system_prompt="", session_id="codex-thread" if resume else None,
        resume=resume, model=None, effort="", workdir=Path("/tmp"),
    )


class Test복귀_확인은_2차_세션에_묶이지_않는다:
    def test_이어가기가_실패하면_새_세션으로_다시_확인한다(self, tmp_path) -> None:
        fallback, primary, switcher = _fallback(tmp_path, [1, 0])
        switcher.begin_switch("auth", engine_name="codex", auto_approve=True)

        response = fallback._probe_primary_recovery(_request(resume=True))

        assert response is not None and response.ok
        assert [r.resume for r in primary.built] == [True, False]
        assert primary.built[1].session_id != "codex-thread"
        assert not switcher.is_switched()

    def test_새_세션도_실패하면_전환을_유지한다(self, tmp_path) -> None:
        fallback, primary, switcher = _fallback(tmp_path, [1, 1])
        switcher.begin_switch("auth", engine_name="codex", auto_approve=True)

        assert fallback._probe_primary_recovery(_request(resume=True)) is None
        assert len(primary.built) == 2
        assert switcher.is_switched()

    def test_새_세션_요청은_한_번만_확인한다(self, tmp_path) -> None:
        fallback, primary, switcher = _fallback(tmp_path, [1])
        switcher.begin_switch("auth", engine_name="codex", auto_approve=True)

        assert fallback._probe_primary_recovery(_request(resume=False)) is None
        assert len(primary.built) == 1


def _response(**kw) -> EngineResponse:
    base = {"ok": True, "body": "b", "session_id": None, "model_actual": None,
            "elapsed": 0.0, "turns": None, "usage": None}
    base.update(kw)
    return EngineResponse(**base)


def _pipeline_with(engine) -> SimpleNamespace:
    return SimpleNamespace(_engine=engine)


class Test하단_모델은_실제로_돈_모델이다:
    _engine = SimpleNamespace(secondary=SimpleNamespace(name="codex"))

    def test_CLI가_알려준_모델을_쓴다(self) -> None:
        shown = RequestPipeline._footer_model(
            _pipeline_with(self._engine), "claude-opus-5-5",
            _response(model_actual="claude-sonnet-5-5", engine="claude"))
        assert shown == "claude-sonnet-5-5"

    def test_대체_실행기_턴은_설정값을_쓰지_않는다(self) -> None:
        shown = RequestPipeline._footer_model(
            _pipeline_with(self._engine), "claude-opus-5-5",
            _response(engine="codex", model_asked="gpt-5.6-sol"))
        assert shown == "gpt-5.6-sol (codex 대체 실행기)"
        assert "claude" not in shown

    def test_모를_때만_설정값을_쓴다(self) -> None:
        shown = RequestPipeline._footer_model(
            _pipeline_with(self._engine), "claude-opus-5-5", _response(engine="claude"))
        assert shown == "claude-opus-5-5"
