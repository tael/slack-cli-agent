"""Cross-engine contract — every Engine implementation must produce the same
observable behavior. Add a new engine by adding one EngineFixture to
ENGINE_FIXTURES; every test in this module runs against it automatically.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from slack_cli_agent.auth.principal import TrustLevel
from slack_cli_agent.config.profile import Profile
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.core.errors import ConfigError
from slack_cli_agent.engine.base import Engine, EngineRequest, Usage
from slack_cli_agent.engine.claude import ClaudeEngine
from slack_cli_agent.engine.codex import CodexEngine
from slack_cli_agent.engine.gemini import GeminiEngine
from slack_cli_agent.engine.runner import EngineRunner
from slack_cli_agent.engine.tool_selection import ToolSelection

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
    # The literal handed to build_command(effort=...) and expected back
    # verbatim in the command. Default is an arbitrary marker string;
    # engines that validate/normalize effort (e.g. Gemini's low/medium/high)
    # override it with a value their own normalization passes through
    # unchanged, instead of the shared test special-casing engines.
    effort_value: str = "고유-effort-값"


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


def _gemini_configured(tmp_path: Path) -> Profile:
    return _profile(
        {"type": "claude", "binary": "claude", "model": "claude-sonnet-5"},
        {"type": "gemini", "binary": "agy", "model": "gemini-3.8-flash", "options": {}},
        tmp_path,
    )


def _gemini_unconfigured(tmp_path: Path) -> Profile:
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


def _gemini_stdout(usage: dict[str, Any]) -> tuple[str, str, int]:
    payload = {
        "conversation_id": "conv-1", "status": "SUCCESS", "response": "제미나이 답변",
        "error": "", "duration_seconds": 1.2, "num_turns": 1, "usage": usage,
    }
    return json.dumps(payload), "", 0


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
        # developer_instructions is pinned to the first turn: the CLI ignores a
        # new value on resume (measured 2026-09-19). So codex carries this turn's
        # instructions in the prompt instead, like gemini (sca-ivs).
        resend_system_prompt_on_resume=True,
        resume_token="resume",
        native_usage={"input_tokens": 5, "output_tokens": 6},
        expected_usage=Usage(
            input_tokens=5, output_tokens=6,
            unavailable=frozenset({"cache_creation_tokens", "cache_read_tokens"}),
        ),
        build_stdout=_codex_stdout,
    ),
    EngineFixture(
        id="gemini",
        engine_class=GeminiEngine,
        configured_profile=_gemini_configured,
        unconfigured_profile=_gemini_unconfigured,
        # No pinned-system-prompt mechanism exists at all (unlike Codex's
        # first-turn-only pin), so the prompt is rebuilt with the system
        # prompt prefixed on every turn, resumed or not.
        resend_system_prompt_on_resume=True,
        resume_token="--conversation",
        native_usage={
            "input_tokens": 7, "output_tokens": 9, "thinking_tokens": 11,
            "cache_read_tokens": 3, "total_tokens": 30,
        },
        expected_usage=Usage(
            input_tokens=7, output_tokens=9, cache_creation_tokens=0, cache_read_tokens=3,
            unavailable=frozenset({"cache_creation_tokens"}),
        ),
        build_stdout=_gemini_stdout,
        # "high" is a valid effort value that agy's normalization passes
        # through unchanged, unlike the shared arbitrary marker string,
        # which would be normalized away (see docs/agy-실측.md 정규화 규칙).
        effort_value="high",
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
        "tools": ToolSelection.allow(["Read", "Grep"]),
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
    def test_로그인_만료_문구와_안내를_댄다(self, fx: EngineFixture, tmp_path: Path) -> None:
        """엔진이 문구를 안 대면 그 엔진만 로그인이 풀려도 전환이 안 된다.
        판정은 Engine 한 자리에 있으므로 엔진이 댈 것은 이 둘뿐이다 (sca-sj8r).
        """
        engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
        assert engine.AUTH_FAILURE_MARKERS, fx.id
        assert "로그인" in engine.AUTH_FAILURE_NOTE, fx.id

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
        cmd = engine.build_command(_request(effort=fx.effort_value))
        assert fx.effort_value in _joined(cmd)

    def test_usage_매핑이_엔진_고유_출력에서_올바로_들어간다(self, fx: EngineFixture, tmp_path: Path) -> None:
        engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
        stdout, stderr, returncode = fx.build_stdout(fx.native_usage)
        resp = engine.parse(stdout, stderr, returncode)
        assert resp.usage == fx.expected_usage

    def test_응답에_실행한_엔진_이름이_박힌다(self, fx: EngineFixture, tmp_path: Path) -> None:
        """보고 쪽이 이 이름으로 기록 리더를 고른다. 비어 있으면 fallback 응답도
        primary 형식으로 읽는다."""
        engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)

        def fake_run(cmd: list[str], cwd: str, timeout: float, **_: Any) -> _FakeCompleted:
            return _FakeCompleted(*fx.build_stdout(fx.native_usage))

        runner = EngineRunner(SETTINGS, subprocess_runner=fake_run)
        assert runner.run(engine, _request()).engine == engine.name

    def test_시간초과_응답에도_엔진_이름이_박힌다(self, fx: EngineFixture, tmp_path: Path) -> None:
        engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)

        def fake_run(cmd: list[str], cwd: str, timeout: float, **_: Any) -> _FakeCompleted:
            raise subprocess.TimeoutExpired(cmd, timeout)

        runner = EngineRunner(SETTINGS, subprocess_runner=fake_run)
        assert runner.run(engine, _request()).engine == engine.name

    def test_설정이_모자라면_ConfigError다(self, fx: EngineFixture, tmp_path: Path) -> None:
        engine = fx.engine_class(fx.unconfigured_profile(tmp_path), SETTINGS)
        with pytest.raises(ConfigError):
            engine.build_command(_request())


def _allowed_tools_params() -> list[Any]:
    params = []
    for fx in ENGINE_FIXTURES:
        marks: tuple[Any, ...] = ()
        if fx.allowed_tools_xfail_reason:
            marks = (pytest.mark.xfail(strict=True, reason=fx.allowed_tools_xfail_reason),)
        params.append(pytest.param(fx, id=fx.id, marks=marks))
    return params


@pytest.mark.parametrize("fx", _allowed_tools_params())
def test_허용_도구_목록이_명령에_반영된다(fx: EngineFixture, tmp_path: Path) -> None:
    """명령에 나타나는 것과 그 엔진이 강제하는 것은 다르다.

    claude 는 --tools 로 도구 집합 자체를 닫는다. codex 와 gemini 는 닫을
    인자가 없어 프롬프트 문구로만 싣는다 (sca-f9k0). 강제 수준의 차이는
    capabilities_for 가 내는 tool_restriction 에 남고, 이 계약은 "요청이
    무엇을 허용했는지가 엔진에 전달은 된다" 까지만 본다.
    """
    engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
    cmd = engine.build_command(_request(tools=ToolSelection.allow(["전용도구A", "전용도구B"])))
    joined = _joined(cmd)
    assert "전용도구A" in joined
    assert "전용도구B" in joined


@pytest.mark.parametrize("fx", _fixture_params())
def test_프롬프트가_하이픈으로_시작해도_플래그로_안_읽힌다(
    fx: EngineFixture, tmp_path: Path
) -> None:
    """사용자가 "--version ..." 같은 말을 던지면 CLI 가 그것을 플래그로 읽는다.
    codex 가 실제로 종료 코드 2 로 죽었다(2026-09-15, 실 CLI 스모크 시험).
    엔진마다 막는 방법이 다르므로 계약은 "프롬프트 바로 앞에 -- 가 있거나
    프롬프트가 값 자리에 있다" 로 둔다."""
    engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
    prompt = "--version 이라는 말은 빼고 답하라"
    cmd = engine.build_command(_request(prompt=prompt))

    # 엔진에 따라 시스템 지침을 프롬프트 앞에 붙이므로 정확히 일치하지 않는다.
    carrier = next(i for i, arg in enumerate(cmd) if prompt in arg)
    assert carrier > 0, "프롬프트가 명령의 첫 인자로 들어갔다"
    assert cmd[carrier - 1] in ("--", "-p"), (
        f"{fx.id}: 프롬프트 앞이 {cmd[carrier - 1]!r} 이다. "
        "하이픈으로 시작하는 프롬프트가 플래그로 읽힌다"
    )


@pytest.mark.parametrize("fx", _fixture_params())
def test_새_세션_id는_그_CLI가_받는_형식이다(fx: EngineFixture, tmp_path: Path) -> None:
    """세 CLI 모두 하이픈 포함 UUID 를 요구한다. 클로드는 하이픈 없는 32자에
    "Error: Invalid session ID. Must be a valid UUID." 로 거부한다(sca-56y 실측).

    형식을 여기서 고정해 둬야, 세션 ID 를 직접 만드는 호출자가 생겼을 때
    무엇을 어겼는지가 드러난다.
    """
    engine = fx.engine_class(fx.configured_profile(tmp_path), SETTINGS)
    session_id = engine.new_session_id()
    assert uuid.UUID(session_id)
    assert str(uuid.UUID(session_id)) == session_id
