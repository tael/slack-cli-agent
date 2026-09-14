"""Cross-engine contract — every Engine implementation must produce the same
observable behavior. Add a new engine by adding one EngineFixture to
ENGINE_FIXTURES; every test in this module runs against it automatically.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from slack_cli_agent.config.profile import Profile
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.errors import ConfigError
from slack_cli_agent.engine.base import Engine, EngineRequest, TrustLevel, Usage
from slack_cli_agent.engine.claude import ClaudeEngine
from slack_cli_agent.engine.codex import CodexEngine
from slack_cli_agent.engine.runner import EngineRunner

SETTINGS = RuntimeSettings()


def _profile(primary: dict[str, Any], fallback: dict[str, Any] | None, tmp_path: Path) -> Profile:
    data: dict[str, Any] = {
        "name": "contract",
        "primary_engine": primary,
        "owner_user_id": "U1",
        "troubleshoot_channel": "C1",
        "state_dir": str(tmp_path / "state"),
    }
    if fallback:
        data["fallback_engine"] = fallback
    return Profile.from_dict(data)


class EngineFixture(NamedTuple):
    id: str
    engine_class: type[Engine]
    configured_profile: Callable[[Path], Profile]
    # A profile where this engine has no block at all — the ConfigError case.
    unconfigured_profile: Callable[[Path], Profile]
    resend_system_prompt_on_resume: bool
    # A token that must be present in the command when resume=True and absent
    # when resume=False (e.g. "--resume" for claude, "resume" for codex).
    resume_token: str
    native_usage: dict[str, Any]
    expected_usage: Usage
    # Builds (stdout, stderr, returncode) for engine.parse() from a native usage dict.
    build_stdout: Callable[[dict[str, Any]], tuple[str, str, int]]
    # None: allowed_tools reflects in the command like other engines.
    # str: known engine gap; xfail(strict=True) with this reason.
    allowed_tools_xfail_reason: str | None = None


def _claude_configured(tmp_path: Path) -> Profile:
    return _profile({"type": "claude", "binary": "claude", "model": "claude-sonnet-5"}, None, tmp_path)


def _claude_unconfigured(tmp_path: Path) -> Profile:
    return _profile({"type": "codex", "binary": "codex", "model": "gpt-5.6-sol"}, None, tmp_path)


def _codex_configured(tmp_path: Path) -> Profile:
    return _profile(
        {"type": "claude", "binary": "claude", "model": "claude-sonnet-5"},
        {"type": "codex", "binary": "codex", "model": "gpt-5.6-sol", "options": {}},
        tmp_path,
    )


def _codex_unconfigured(tmp_path: Path) -> Profile:
    return _profile({"type": "claude", "binary": "claude", "model": "claude-sonnet-5"}, None, tmp_path)


def _claude_stdout(usage: dict[str, Any]) -> tuple[str, str, int]:
    payload = {
        "result": "답변 본문", "session_id": "s1", "model": "claude-sonnet-5",
        "num_turns": 1, "usage": usage, "is_error": False,
    }
    return json.dumps(payload), "", 0


def _codex_stdout(usage: dict[str, Any]) -> tuple[str, str, int]:
    lines = [
        json.dumps({"type": "thread.started", "thread_id": "th-1"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "코덱스 답변"}}),
        json.dumps({"type": "turn.completed", "usage": usage}),
    ]
    return "\n".join(lines), "", 0


ENGINE_FIXTURES: list[EngineFixture] = [
    EngineFixture(
        id="claude",
        engine_class=ClaudeEngine,
        configured_profile=_claude_configured,
        unconfigured_profile=_claude_unconfigured,
        resend_system_prompt_on_resume=True,
        resume_token="--resume",
        native_usage={
            "input_tokens": 10, "output_tokens": 20,
            "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4,
        },
        expected_usage=Usage(input_tokens=10, output_tokens=20, cache_creation_tokens=3, cache_read_tokens=4),
        build_stdout=_claude_stdout,
    ),
    EngineFixture(
        id="codex",
        engine_class=CodexEngine,
        configured_profile=_codex_configured,
        unconfigured_profile=_codex_unconfigured,
        # Codex pins the system prompt on the first turn only (confirmed 2026-09-11,
        # see codex.py module docstring) — a resend on resume is silently ignored by
        # the CLI, so this engine doesn't attempt it.
        resend_system_prompt_on_resume=False,
        resume_token="resume",
        native_usage={"input_tokens": 5, "output_tokens": 6},
        expected_usage=Usage(input_tokens=5, output_tokens=6),
        build_stdout=_codex_stdout,
        allowed_tools_xfail_reason=(
            "Codex CLI has no tool-allowlist flag; sandbox mode governs execution "
            "scope instead (checked 2026-09-15)."
        ),
    ),
]


def _fixture_params() -> list[Any]:
    return [pytest.param(fx, id=fx.id) for fx in ENGINE_FIXTURES]


def _request(**overrides: Any) -> EngineRequest:
    base: dict[str, Any] = {
        "prompt": "안녕",
        "system_prompt": "시스템 지침",
        "session_id": "11111111-1111-1111-1111-111111111111",
        "resume": False,
        "model": "engine-model-x",
        "effort": "medium",
        "workdir": Path("/tmp/contract-work"),
        "readable_dirs": (Path("/tmp/read-a"), Path("/tmp/read-b")),
        "allowed_tools": ("Read", "Grep"),
        "trust_level": TrustLevel.GENERAL,
    }
    base.update(overrides)
    return EngineRequest(**base)


def _joined(cmd: list[str]) -> str:
    # Keep token boundaries so substring checks can't false-match across tokens.
    return "\x1f".join(cmd)


class _FakeCompleted:
    def __init__(self, stdout: str, stderr: str, returncode: int) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@pytest.mark.parametrize("fx", _fixture_params())
class TestEngineContract:
    def test_재개_여부가_실제로_다른_명령을_만든다(self, fx: EngineFixture, tmp_path: Path) -> None:
        engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
        cmd_new = engine.build_command(_request(resume=False))
        cmd_resume = engine.build_command(_request(resume=True))
        assert cmd_new != cmd_resume

    def test_재개_세션은_세션_id를_명령에_담는다(self, fx: EngineFixture, tmp_path: Path) -> None:
        engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
        cmd = engine.build_command(_request(resume=True, session_id="고유-세션-id-값"))
        assert "고유-세션-id-값" in _joined(cmd)

    def test_재개_전용_토큰은_resume일때만_나온다(self, fx: EngineFixture, tmp_path: Path) -> None:
        engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
        cmd_new = engine.build_command(_request(resume=False))
        cmd_resume = engine.build_command(_request(resume=True))
        assert fx.resume_token in cmd_resume
        assert fx.resume_token not in cmd_new

    def test_시스템_프롬프트는_새_세션_명령에_실린다(self, fx: EngineFixture, tmp_path: Path) -> None:
        engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
        cmd = engine.build_command(_request(resume=False, system_prompt="고유한-지침-문구"))
        assert "고유한-지침-문구" in _joined(cmd)

    def test_재개_세션의_시스템_프롬프트_반영_여부는_엔진_설계대로다(
        self, fx: EngineFixture, tmp_path: Path
    ) -> None:
        engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
        cmd = engine.build_command(_request(resume=True, system_prompt="고유한-지침-문구"))
        assert ("고유한-지침-문구" in _joined(cmd)) == fx.resend_system_prompt_on_resume

    def test_작업_디렉터리는_실행기가_그대로_넘긴다(self, fx: EngineFixture, tmp_path: Path) -> None:
        engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
        seen: dict[str, Any] = {}

        def fake_run(cmd: list[str], cwd: str, timeout: float, **_: Any) -> _FakeCompleted:
            seen["cwd"] = cwd
            return _FakeCompleted(*fx.build_stdout(fx.native_usage))

        runner = EngineRunner(SETTINGS, subprocess_runner=fake_run)
        workdir = tmp_path / "workdir-반영-확인"
        runner.run(engine, _request(workdir=workdir))
        assert seen["cwd"] == str(workdir)

    def test_읽기_허용_경로가_명령에_반영된다(self, fx: EngineFixture, tmp_path: Path) -> None:
        engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
        dirs = (Path("/tmp/read-only-a-고유"), Path("/tmp/read-only-b-고유"))
        cmd = engine.build_command(_request(resume=False, readable_dirs=dirs))
        joined = _joined(cmd)
        assert "/tmp/read-only-a-고유" in joined
        assert "/tmp/read-only-b-고유" in joined

    def test_모델이_명령에_반영된다(self, fx: EngineFixture, tmp_path: Path) -> None:
        engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
        cmd = engine.build_command(_request(model="고유-모델-이름-9"))
        assert "고유-모델-이름-9" in _joined(cmd)

    def test_effort가_명령에_반영된다(self, fx: EngineFixture, tmp_path: Path) -> None:
        engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
        cmd = engine.build_command(_request(effort="고유-effort-값"))
        assert "고유-effort-값" in _joined(cmd)

    def test_usage_매핑이_엔진_고유_출력에서_올바로_들어간다(self, fx: EngineFixture, tmp_path: Path) -> None:
        engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
        stdout, stderr, returncode = fx.build_stdout(fx.native_usage)
        resp = engine.parse(stdout, stderr, returncode)
        assert resp.usage == fx.expected_usage

    def test_설정이_모자라면_ConfigError다(self, fx: EngineFixture, tmp_path: Path) -> None:
        engine = fx.engine_class(fx.unconfigured_profile(tmp_path), SETTINGS)
        with pytest.raises(ConfigError):
            engine.build_command(_request())


def _allowed_tools_params() -> list[Any]:
    params = []
    for fx in ENGINE_FIXTURES:
        marks = ()
        if fx.allowed_tools_xfail_reason:
            marks = (pytest.mark.xfail(strict=True, reason=fx.allowed_tools_xfail_reason),)
        params.append(pytest.param(fx, id=fx.id, marks=marks))
    return params


@pytest.mark.parametrize("fx", _allowed_tools_params())
def test_허용_도구_목록이_명령에_반영된다(fx: EngineFixture, tmp_path: Path) -> None:
    engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
    cmd = engine.build_command(_request(allowed_tools=("전용도구A", "전용도구B")))
    joined = _joined(cmd)
    assert "전용도구A" in joined
    assert "전용도구B" in joined
